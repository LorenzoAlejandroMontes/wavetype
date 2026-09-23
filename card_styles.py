"""
card_styles.py — i tre stili della card live di Wavetype, portati dai mockup approvati (19/09).

  stamp  (A)  neo-brutalista: bordi neri spessi, ombra piena, testata a colore per stato
  glyph  (B)  ispirato a Nothing: nero puro, puntini, un solo rosso, matrice di punti
  signal (D)  strumento: oscilloscopio vivo sul lato che guarda il cursore

Fonte delle misure: design/a_brutal.html, b_nothing.html, d_signal.html + common.css
(scratchpad della sessione del 19/09). Tutte le misure qui sono in px CSS (logici a 96 dpi):
si moltiplicano per la scala del monitor al momento del disegno.

Un solo cambio rispetto al mockup: in Glyph il testo "annullato" tutto in
carattere a punti si leggeva male. Il carattere a punti resta solo per la parola barrata e il
timer; il corpo resta nel sans (Space Grotesk).

Divisione del lavoro con live_panel.py:
  - qui: font, colori, misure, e il disegno di cio' che NON e' testo dettato
    (lastra della card, testata, pie' con i tasti, meter, timer, barra di avanzamento, pillola)
  - live_panel.py: parole, diff, animazioni, posizione, finestra.
Tutto cio' che non cambia da un frame all'altro (lastra, ombra, texture a punti, pie') si
costruisce una volta e resta in cache: per frame si disegnano solo meter, timer e parole.
"""
import math
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import paths

ROOT = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = paths.resource(os.path.join("assets", "fonts"), ROOT)

STYLES = ("stamp", "glyph", "signal")
DEFAULT_STYLE = "signal"
TITLES = {"stamp": "Stamp", "glyph": "Glyph", "signal": "Signal"}
PHASES = ("listening", "live", "paused", "formatting", "inserted", "cancelled", "offline",
          "recovering", "recovered", "no_audio", "unrecovered")
# Win+Ctrl+R: le quattro fasi del recupero dell'ultima registrazione. Stanno in PHASES (stesso
# corpo della dettatura: le parole recuperate si vedono come quelle dettate), senza il prefisso
# di Edit perche' non cambiano la forma della card, solo testata e pie'.
RECOVER_PHASES = ("recovering", "recovered", "no_audio", "unrecovered")
FOOT = ("paused", "cancelled", "offline", "no_audio", "unrecovered")   # fasi col pie' di pagina
NO_TIMER = ("cancelled", "no_audio")                                   # fasi senza il tempo a destra
# pie' di pagina del recupero: (sinistra, tasto, destra). Copy inglese, nessun carattere difensivo.
R_FOOTERS = {
    "no_audio": ("Dictate once, then", "Win+Ctrl+R", "brings it back"),
    "unrecovered": ("Audio kept", "Win+Ctrl+R", "try again"),
}

# file dei font (OFL, sottoinsieme "latin" di Google Fonts: vedi assets/fonts/README.txt)
FONT_FILES = {
    "bricolage": "BricolageGrotesque-var-latin.woff2",   # assi opsz 12-96, wght 200-800
    "jbmono": "JetBrainsMono-var-latin.woff2",           # wght 400-800
    "doto": "Doto-var-latin.woff2",                      # wght 100-900 (dot-matrix)
    "sgrotesk": "SpaceGrotesk-var-latin.woff2",          # wght 300-700
    "smono": "SpaceMono-Regular-latin.woff2",
    "smono-b": "SpaceMono-Bold-latin.woff2",
    "geist": "Geist-var-latin.woff2",                    # wght 100-900
    "gmono": "GeistMono-var-latin.woff2",                # wght 100-900
}

_FONTS = {}


def _fallback_font(px):
    fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for name in ("SegUIVar.ttf", "segoeui.ttf", "arial.ttf"):
        p = os.path.join(fonts, name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, px)
            except Exception:
                continue
    return ImageFont.load_default()


def font(family, px, wght=None, opsz=None):
    """Font in cache per (famiglia, misura, peso). Se il file manca: Segoe UI (mai un'eccezione)."""
    px = max(6.0, round(float(px), 2))
    key = (family, px, wght, opsz)
    f = _FONTS.get(key)
    if f is not None:
        return f
    try:
        f = ImageFont.truetype(os.path.join(FONT_DIR, FONT_FILES[family]), px)
        try:
            axes = f.get_variation_axes()
        except Exception:
            axes = None
        if axes:
            vals = []
            for a in axes:
                name = a.get("name", b"")
                name = name.decode("ascii", "ignore") if isinstance(name, bytes) else str(name)
                v = a.get("default")
                if name.lower().startswith("weight") and wght is not None:
                    v = wght
                elif name.lower().startswith("optical") and opsz is not None:
                    v = opsz
                vals.append(max(a["minimum"], min(a["maximum"], v)))
            f.set_variation_by_axes(vals)
    except Exception:
        f = _fallback_font(px)
    _FONTS[key] = f
    return f


# ---------------------------------------------------------------- copertura glifi
_COVER = {}


_LATIN_EXTRA = set("–—‘’‚“”„…€·•™ıŒœ")


def covers(f, text):
    """True se il font ha un glifo per ogni carattere. I file sono il sottoinsieme "latin" di
    Google Fonts (U+0000-00FF e pochi altri): una parola in polacco o in greco va disegnata col
    font di ripiego, non a quadratini. Il controllo si fa una volta per carattere e resta in cache."""
    for c in text:
        if ord(c) < 0x100 or c in _LATIN_EXTRA:
            continue
        k = (id(f), c)
        ok = _COVER.get(k)
        if ok is None:
            try:
                nd = f.getmask("￿").getbbox(), f.getlength("￿")
                ok = not (f.getmask(c).getbbox() == nd[0] and f.getlength(c) == nd[1])
            except Exception:
                ok = True
            _COVER[k] = ok
        if not ok:
            return False
    return True


def fallback_for(f):
    try:
        px = f.size
    except Exception:
        px = 16
    return font_fallback(px)


def font_fallback(px):
    key = ("fallback", round(float(px), 2))
    f = _FONTS.get(key)
    if f is None:
        f = _FONTS[key] = _fallback_font(px)
    return f


# ---------------------------------------------------------------- primitive di disegno
def hexc(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


_CORNERS = {}
_MASKS = {}


def _corner(r):
    """Quarto di disco antialiasato (in alto a sinistra), supercampionato 4x."""
    r = max(1, int(r))
    c = _CORNERS.get(r)
    if c is None:
        S = 4
        big = Image.new("L", (r * S, r * S), 0)
        ImageDraw.Draw(big).ellipse([0, 0, 2 * r * S - 1, 2 * r * S - 1], fill=255)
        c = _CORNERS[r] = big.resize((r, r), Image.LANCZOS)
    return c


def rr_mask(w, h, r):
    """Maschera di un rettangolo arrotondato per 9 pezzi: nessun supercampionamento
    dell'intera area (costava ~10 ms a ogni misura nuova, ora ~0,2)."""
    w, h = max(1, int(w)), max(1, int(h))
    r = int(max(0, min(r, w // 2, h // 2)))
    key = (w, h, r)
    m = _MASKS.get(key)
    if m is not None:
        return m
    m = Image.new("L", (w, h), 255)
    if r > 0:
        c = _corner(r)
        m.paste(c, (0, 0))
        m.paste(c.transpose(Image.FLIP_LEFT_RIGHT), (w - r, 0))
        m.paste(c.transpose(Image.FLIP_TOP_BOTTOM), (0, h - r))
        m.paste(c.transpose(Image.ROTATE_180), (w - r, h - r))
    if len(_MASKS) > 600:
        _MASKS.clear()
    _MASKS[key] = m
    return m


def solid(size, rgb, mask, alpha=1.0):
    """Immagine RGBA di colore pieno con la maschera come alpha (moltiplicata per alpha)."""
    img = Image.new("RGBA", size, tuple(rgb) + (0,))
    if alpha < 0.999:
        mask = mask.point(lambda v, a=alpha: int(v * a + 0.5))
    img.putalpha(mask)
    return img


def _erf(x):
    """erf vettoriale (Abramowitz-Stegun 7.1.26, errore < 1,5e-7): basta e avanza per un'ombra."""
    sgn = np.sign(x)
    x = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t
               + 0.254829592) * t * np.exp(-x * x)
    return sgn * y


def _profile(n, a, b, sigma, fade=12):
    """Profilo 1D di un segmento [a, b] sfocato con una gaussiana (erf): esatto e veloce.
    Negli ultimi `fade` px verso il bordo del bitmap scende a zero: la coda dell'ombra (~1%)
    non deve finire tagliata di netto sul bordo della finestra (visto a contrasto alzato)."""
    x = np.arange(n, dtype=np.float32) + 0.5
    k = 1.0 / (max(0.3, sigma) * math.sqrt(2.0))
    p = (0.5 * (_erf((x - a) * k) - _erf((x - b) * k))).astype(np.float32)
    if fade and n > 2 * fade:
        ramp = np.clip(np.minimum(x, n - x) / fade, 0, 1).astype(np.float32)
        p *= ramp * ramp * (3 - 2 * ramp)
    return p


def box_shadow(W, H, x0, y0, x1, y1, sigma, alpha):
    """Alpha (float 0..1, H x W) di box-shadow di un rettangolo [x0,x1]x[y0,y1] con sfocatura sigma.
    La sfocatura gaussiana di un rettangolo e' separabile: prodotto di due profili erf.
    Gli angoli arrotondati (12-18 px) sotto una sfocatura di 34-36 px non si distinguono.
    Costa ~1 ms a qualunque misura (la sfocatura vera costava ~10 ms per misura nuova)."""
    return np.outer(_profile(H, y0, y1, sigma) * np.float32(alpha), _profile(W, x0, x1, sigma))


def shadow_image(W, H, layers):
    """Immagine RGBA nera con le ombre sovrapposte (alpha 'over')."""
    keep = None
    for (x0, y0, x1, y1, sigma, alpha) in layers:
        k = 1.0 - box_shadow(W, H, x0, y0, x1, y1, sigma, alpha)
        keep = k if keep is None else keep * k
    a8 = ((1.0 - keep) * 255.0 + 0.5).astype(np.uint8)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    img.putalpha(Image.fromarray(a8, "L"))
    return img


def text_mask(text, f, tracking=0.0):
    """Maschera L del testo, alta ascent+descent (la stessa scatola che CSS centra sulla riga).
    Con tracking (letter-spacing) si disegna carattere per carattere."""
    asc, desc = f.getmetrics()
    h = asc + desc
    if not tracking:
        w = max(1, int(math.ceil(f.getlength(text))) + 2)
        m = Image.new("L", (w, h), 0)
        ImageDraw.Draw(m).text((0, 0), text, font=f, fill=255, anchor="la")
        return m, f.getlength(text)
    adv = sum(f.getlength(c) + tracking for c in text)
    w = max(1, int(math.ceil(adv)) + 2)
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    x = 0.0
    for c in text:
        d.text((x, 0), c, font=f, fill=255, anchor="la")
        x += f.getlength(c) + tracking
    return m, adv - tracking          # CSS mette lo spazio anche dopo l'ultima: non conta per l'allineamento


def slice9(ref, W, H, ls, rs, ts, bs, pw=1):
    """Ricompone `ref` alla misura W x H: angoli copiati, bordi e centro ripetuti.
    In orizzontale il centro si ripete con passo `pw` (tratteggi, in fase col bordo sinistro).
    Torna None se la misura e' troppo piccola per le fette."""
    RW, RH = ref.size
    if W < ls + rs + 1 or H < ts + bs + 1 or RW < ls + rs + pw or RH < ts + bs + 1:
        return None
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    rows = [(0, ts, 0, ts), (RH // 2, RH // 2 + 1, ts, H - bs), (RH - bs, RH, H - bs, H)]
    for sy0, sy1, dy0, dy1 in rows:
        if dy1 <= dy0:
            continue
        strip = ref.crop((0, sy0, RW, sy1))
        if sy1 - sy0 != dy1 - dy0:
            strip = strip.resize((RW, dy1 - dy0), Image.NEAREST)
        if W == RW:
            out.paste(strip, (0, dy0))
            continue
        out.paste(strip.crop((0, 0, ls, strip.height)), (0, dy0))
        out.paste(strip.crop((RW - rs, 0, RW, strip.height)), (W - rs, dy0))
        mid = W - ls - rs
        if mid > 0:
            if pw == 1:
                col = strip.crop((RW // 2, 0, RW // 2 + 1, strip.height))
                out.paste(col.resize((mid, strip.height), Image.NEAREST), (ls, dy0))
            else:
                x0 = RW // 2
                x0 -= (x0 - ls) % pw
                band = np.asarray(strip.crop((x0, 0, x0 + pw, strip.height)))
                reps = mid // pw + 1
                out.paste(Image.fromarray(np.ascontiguousarray(np.tile(band, (1, reps, 1))[:, :mid]), "RGBA"),
                          (ls, dy0))
    return out


class Label:
    """Testo corto (etichette, tasti, timer) gia' pronto come sprite RGBA, in cache."""
    _cache = {}

    @classmethod
    def get(cls, text, f, rgb, tracking=0.0, alpha=1.0):
        key = (text, id(f), tuple(rgb), round(tracking, 2), round(alpha, 3))
        sp = cls._cache.get(key)
        if sp is None:
            m, adv = text_mask(text, f, tracking)
            sp = (solid(m.size, rgb, m, alpha), adv)
            if len(cls._cache) > 800:
                cls._cache.clear()
            cls._cache[key] = sp
        return sp


def blit(dst, sp, x, y):
    """alpha_composite che tollera coordinate negative o fuori dal bordo."""
    x, y = int(round(x)), int(round(y))
    if x >= dst.width or y >= dst.height or x + sp.width <= 0 or y + sp.height <= 0:
        return
    if x < 0 or y < 0:
        sp = sp.crop((max(0, -x), max(0, -y), sp.width, sp.height))
        x, y = max(0, x), max(0, y)
    dst.alpha_composite(sp, (x, y))


def text_center(dst, text, f, rgb, x, cy, tracking=0.0, alpha=1.0, right=False):
    """Scrive il testo centrato in verticale su cy. Torna la larghezza."""
    sp, adv = Label.get(text, f, rgb, tracking, alpha)
    if right:
        x = x - adv
    blit(dst, sp, x, cy - sp.height / 2.0)
    return adv


def label_width(text, f, tracking=0.0):
    return Label.get(text, f, (255, 255, 255), tracking)[1]


def disc(r, rgb, alpha=1.0):
    """Pallino antialiasato di raggio r (px fisici), in cache."""
    key = ("disc", round(r, 2), tuple(rgb), round(alpha, 3))
    sp = _MASKS.get(key)
    if sp is None:
        S = 4
        side = int(math.ceil(r * 2)) + 2
        big = Image.new("L", (side * S, side * S), 0)
        c = side * S / 2.0
        ImageDraw.Draw(big).ellipse([c - r * S, c - r * S, c + r * S, c + r * S], fill=255)
        sp = solid((side, side), rgb, big.resize((side, side), Image.LANCZOS), alpha)
        _MASKS[key] = sp
    return sp


def glow_disc(r, blur, rgb, alpha):
    """Alone sfocato attorno a un pallino (box-shadow 0 0 blur)."""
    key = ("glow", round(r, 2), round(blur, 2), tuple(rgb), round(alpha, 3))
    sp = _MASKS.get(key)
    if sp is None:
        pad = int(blur * 2) + 2
        side = int(r * 2) + 2 * pad
        m = Image.new("L", (side, side), 0)
        c = side / 2.0
        ImageDraw.Draw(m).ellipse([c - r, c - r, c + r, c + r], fill=255)
        m = m.filter(ImageFilter.GaussianBlur(blur / 2.0))
        sp = solid((side, side), rgb, m, alpha)
        _MASKS[key] = sp
    return sp


def put_center(dst, sp, cx, cy):
    blit(dst, sp, cx - sp.width / 2.0, cy - sp.height / 2.0)


def fmt_time(sec, pad=False):
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    t = f"{m}:{s:02d}"
    return t.rjust(5, "0") if pad else t


# ---------------------------------------------------------------- Edit Mode (comune ai tre stili)
# La card di Edit e' la stessa lastra della dettatura con un corpo diverso: riga dei chip,
# riga "YOU ..." con l'istruzione. Le fasi hanno il prefisso "e_" cosi' non incrociano mai
# quelle della dettatura (PHASES): ogni cache che ha la fase nella chiave resta separata.
EDIT_MODES = ("e_listening", "e_rewriting", "e_done", "e_cancelled", "e_offline", "e_nothing")
E_SAID = ("e_listening",)                      # le fasi con la riga dell'istruzione
E_CHIPS_ALL = ("e_listening",)                 # tutti e tre i chip
E_CHIPS_ONE = ("e_rewriting", "e_offline")     # solo il chip scelto (nessuno se libera)
E_FOOT = ("e_cancelled", "e_offline", "e_nothing")
E_HINT = "Say it, or press 1-3"
E_YOU = "You"

# pie' di pagina: (sinistra, tasto, destra). Copy inglese, nessun carattere difensivo.
E_FOOTERS = {
    "e_cancelled": ("Selection untouched", "", ""),
    "e_offline": ("Selection unchanged", "Win+Ctrl", "try again"),
    "e_nothing": ("Select text, then", "Win+Ctrl", "edit"),
}


def sel_mark(k, w, h, bg, fg, bar=2.0, y1=3.5, y2=7.5):
    """Icona "testo selezionato" (.selmk del mockup): rettangolino pieno con due righe dentro,
    la seconda piu' corta. In cache: e' ferma per tutta la sessione."""
    key = ("selmk", round(k, 3), w, h, tuple(bg), tuple(fg), bar, y1, y2)
    sp = _MASKS.get(key)
    if sp is None:
        W, H = max(1, int(round(w * k))), max(1, int(round(h * k)))
        sp = solid((W, H), bg, rr_mask(W, H, max(1, int(round(2 * k)))))
        bh = max(1, int(round(bar * k)))
        for top, right in ((y1, 3.0), (y2, 6.0)):
            x0, x1 = int(round(3 * k)), int(round((w - right) * k))
            if x1 > x0:
                blit(sp, solid((x1 - x0, bh), fg, rr_mask(x1 - x0, bh, bh // 2)), x0, int(round(top * k)))
        if len(_MASKS) > 600:
            _MASKS.clear()
        _MASKS[key] = sp
    return sp


# ---------------------------------------------------------------- stile base
class Style:
    """Interfaccia comune. Misure in px CSS; `k` = scala del monitor."""
    name = "base"
    card_w = 480
    radius = 12
    line_h = 25
    text_inset = 20          # dal bordo sinistro della card alla colonna del testo
    pad_r = 20               # dal testo al bordo destro
    margin = 36              # spazio attorno alla card nel bitmap (ombra)
    strike_pad = 0           # margine orizzontale della parola barrata (Stamp: 2)
    # dettatura lunga: maschera in cima al corpo (mask-image del mockup), [(y px CSS, alpha)]
    over_mask = ((0, 0.0), (34, 1.0))

    # colori delle parole
    c_commit = (255, 255, 255)
    c_tent = (128, 128, 128)
    c_gone = (128, 128, 128)
    c_cancel = (128, 128, 128)
    c_after = None           # Signal: testo non ancora "passato" dal fascio in formattazione

    def __init__(self, k=1.0):
        self.k = k

    def px(self, v):
        return v * self.k

    def ipx(self, v):
        return int(round(v * self.k))

    def backdrop(self, M, cw, ch, r, draw):
        """Ombre e anelli attorno alla card: dipendono solo dalla misura, non dalla fase.
        In cache a parte, cosi' pausa/formattazione/annullo non rifanno la sfocatura."""
        key = ("bd", self.name, self.k, M, cw, ch, r)
        img = _MASKS.get(key)
        if img is None:
            img = Image.new("RGBA", (cw + 2 * M, ch + 2 * M), (0, 0, 0, 0))
            draw(img, cw, ch)
            if len(_MASKS) > 600:
                _MASKS.clear()
            _MASKS[key] = img
        return img.copy()

    # font del corpo
    def body_font(self):
        raise NotImplementedError

    def struck_font(self):
        return self.body_font()

    # zone verticali: (alto_fisso, pad_top, pad_bot, basso_fisso) in px CSS, con m = 0 compatta, 1 piena
    def zones(self, mode, above, m=1.0):
        raise NotImplementedError

    def compact_size(self, panel, mode):
        """(w, h) in px CSS della card compatta ("listening")."""
        raise NotImplementedError

    def max_height(self):
        """Altezza massima della card piena (3 righe + pie'): serve a _plan()."""
        raise NotImplementedError

    # ------------------------------------------------------------ Edit Mode (comune)
    # Misure in px CSS: ogni stile rimette i suoi numeri. Il resto (segmenti di pari
    # larghezza, regola della card stretta) e' uguale per i tre stili.
    E_CHIP_PAD = 12          # dal bordo alto del corpo alla riga dei chip
    E_CHIP_H = 32            # altezza del chip
    E_CHIP_GAP = 8           # tra un chip e l'altro
    E_CHIP_BOT = 14          # dalla riga dei chip a quello che viene dopo
    E_CHIP_SIDE = 10         # padding orizzontale dentro al chip
    E_CHIP_IN = 7            # tra il numero e l'etichetta
    E_KBD_W = 18             # larghezza del riquadro del numero
    edit_line_h = 22         # interlinea della riga "YOU ..."
    edit_inset = 20          # dal bordo sinistro della card alla colonna dei chip
    edit_pad_r = 20
    edit_you_gap = 8         # tra l'etichetta YOU e l'istruzione
    ec_commit = (255, 255, 255)
    ec_tent = (128, 128, 128)
    ec_gone = (128, 128, 128)
    ec_ph = (128, 128, 128)
    ec_you = (128, 128, 128)

    def zones_of(self, mode, above, m=1.0, chip=True):
        """Le zone verticali della fase, dettatura o Edit. `chip`: in Edit, se un chip e' scelto
        (senza, "riscrivo" e "offline" non tengono il posto di una riga vuota)."""
        if mode[:2] == "e_":
            return self.edit_zones(mode, above, m, chip)
        return self.zones(mode, above, m)

    def has_foot(self, mode):
        return mode in E_FOOT if mode[:2] == "e_" else mode in FOOT

    def edit_chips_h(self):
        return self.E_CHIP_PAD + self.E_CHIP_H + self.E_CHIP_BOT

    def edit_chip_font(self):
        raise NotImplementedError

    def edit_kbd_w(self):
        return self.px(self.E_KBD_W)

    def chip_text_w(self, label):
        return label_width(label, self.edit_chip_font(), self.edit_chip_tr())

    def edit_chip_tr(self):
        return 0.0

    def chip_w(self, chip, full=True):
        """Larghezza naturale di UN chip (px fisici)."""
        w = 2 * self.px(self.E_CHIP_SIDE) + self.edit_kbd_w()
        if full:
            w += self.px(self.E_CHIP_IN) + self.chip_text_w(chip.label)
        return w

    def chip_seg(self, w):
        """Larghezza di un segmento quando i tre chip riempiono la riga larga `w`."""
        return (w - 2 * self.px(self.E_CHIP_GAP)) / 3.0

    def chip_labels_fit(self, chips, seg):
        """La parola ci sta in un segmento? Se no si tiene solo il numero (card stretta).
        Misurata sul chip piu' largo: o ci stanno tutte e tre o nessuna (la riga resta pari)."""
        need = max(self.chip_w(c, True) for c in chips)
        return need <= seg + 0.5

    def edit_chip_row(self, img, x0, y, w, chips, lit=None, dim_all=False, only=None, tab=None):
        """La riga dei chip: tre segmenti di pari larghezza, oppure il solo chip scelto.
        `lit` = chip acceso; `dim_all` = istruzione libera (tutti spenti)."""
        h = self.px(self.E_CHIP_H)
        if only is not None:
            self._chip(img, x0, y, self.chip_w(only, True), h, only, "on", True, tab)
            return
        gap = self.px(self.E_CHIP_GAP)
        seg = self.chip_seg(w)
        full = self.chip_labels_fit(chips, seg)
        for i, c in enumerate(chips):
            if lit:
                state = "on" if c.id == lit else "off"
            else:
                state = "off" if dim_all else "base"
            self._chip(img, x0 + i * (seg + gap), y, seg, h, c, state, full, tab)


# ================================================================= A · STAMP
INK = hexc("#0B0B0B")
PAPER = hexc("#FFFDF6")
A_TAB = {"live": "#FFD83D", "listening": "#FFD83D", "paused": "#EDE8DA", "formatting": "#C9B6FF",
         "cancelled": "#FF6B4A", "offline": "#FFB23F", "preview": "#FFD83D",
         "recovering": "#C9B6FF", "recovered": "#B6F36A", "no_audio": "#EDE8DA",
         "unrecovered": "#FFB23F"}
A_CUT = hexc("#FF4F2B")
A_FIXED = hexc("#B6F36A")
A_FORMAT = hexc("#C9B6FF")


class Stamp(Style):
    name = "stamp"
    radius = 10
    B = 2.5                  # bordo
    HD = 38                  # testata
    text_inset = 2.5 + 18
    pad_r = 2.5 + 18
    margin = 12
    strike_pad = 2
    over_mask = ((0, 0.0), (14, 0.35), (40, 1.0))
    c_commit = INK
    c_tent = hexc("#8C887B")
    c_gone = hexc("#9A968A")
    c_cancel = hexc("#9A968A")

    def b(self):
        return max(1, self.ipx(self.B))

    def body_font(self):
        return font("bricolage", self.px(17), wght=500, opsz=17)

    def lab_font(self):
        return font("bricolage", self.px(13), wght=800, opsz=13)

    def ft_font(self):
        return font("bricolage", self.px(13), wght=600, opsz=13)

    def key_font(self):
        return font("jbmono", self.px(11.5), wght=700)

    def tm_font(self):
        return font("jbmono", self.px(12.5), wght=700)

    def zones(self, mode, above, m=1.0):
        top = self.B + self.HD + self.B
        bot = self.B
        if self.has_foot(mode):
            bot += self.B + 10 + 24 + 10
        elif mode == "formatting":
            bot += 12 + 16
        return top, 12, 15, bot

    def max_height(self):
        t, pt, pb, _ = self.zones("live", True)
        return t + pt + 3 * self.line_h + pb + self.B + 10 + 24 + 10 + self.B

    def _label(self, mode, panel):
        if panel.preview_name:
            return panel.preview_name.upper(), True
        return {"listening": ("LISTENING", True), "live": ("REC", True), "paused": ("PAUSED", True),
                "formatting": ("FORMATTING", False), "cancelled": ("CANCELLED", False),
                "offline": ("REC · OFFLINE", True),
                "recovering": ("RECOVERING", False), "recovered": ("RECOVERED", False),
                "no_audio": ("NOTHING YET", False),
                "unrecovered": ("OFFLINE", False)}.get(mode, ("REC", True))

    def compact_size(self, panel, mode):
        if mode == "recovering":                    # niente barre e niente timer: label + durata
            lab, _ = self._label(mode, panel)
            lw = label_width(lab + " · " + panel.timer_text(), self.lab_font(),
                             self.px(13 * 0.02)) / self.k
            return self.B + 14 + lw + 14 + self.B, self.B + self.HD + self.B
        lab, dot = self._label("listening", panel)
        lw = label_width(lab, self.lab_font(), self.px(13 * 0.02)) / self.k
        w = self.B + 14 + (11 + 7 if dot else 0) + lw + 10 + 4 + (9 * 4 + 8 * 3) + 10 + self.B
        return w, self.B + self.HD + self.B

    # -------- lastra (in cache): bordo, testata, pie'
    def plate(self, panel, mode, cw, ch, above, m):
        k = self.k
        b = self.b()
        r = self.ipx(self.radius)
        tab = hexc(self.tab_for(mode))
        hd = self.ipx(self.HD)
        card = Image.new("RGBA", (cw, ch), INK + (255,))
        iw, ih = max(1, cw - 2 * b), max(1, ch - 2 * b)
        inner = Image.new("RGBA", (iw, ih), PAPER + (255,))
        d = ImageDraw.Draw(inner)
        d.rectangle([0, 0, iw, hd - 1], fill=tab + (255,))
        if ih > hd + b:                                        # c'e' un corpo: filo sotto la testata
            d.rectangle([0, hd, iw, hd + b - 1], fill=INK + (255,))
        if self.has_foot(mode) and ih > hd + b:
            fh = self.ipx(10 + 24 + 10)
            fy = ih - fh
            d.rectangle([0, fy - b, iw, fy - 1], fill=INK + (255,))
            d.rectangle([0, fy, iw, ih], fill=(255, 255, 255, 255))
            self._footer(inner, mode, fy, fh)
        if mode == "formatting" and ih > hd + b:
            pass                                               # la barra e' animata: in front()
        inner.putalpha(rr_mask(iw, ih, max(0, r - b)))
        card.alpha_composite(inner, (b, b))
        card.putalpha(rr_mask(cw, ch, r))
        # ombre: alone bianco sfalsato, anello bianco, ombra piena nera (l'ordine CSS rovesciato)
        M = panel.margin

        def draw(img, cw, ch):
            o6 = self.ipx(6.5)
            o5 = self.ipx(5)
            ri = int(round(self.px(1.5)))
            img.alpha_composite(solid((cw, ch), (255, 255, 255), rr_mask(cw, ch, r), 0.85), (M + o6, M + o6))
            img.alpha_composite(solid((cw + 2 * ri, ch + 2 * ri), (255, 255, 255),
                                      rr_mask(cw + 2 * ri, ch + 2 * ri, r + ri), 0.85), (M - ri, M - ri))
            img.alpha_composite(solid((cw, ch), INK, rr_mask(cw, ch, r)), (M + o5, M + o5))
        img = self.backdrop(M, cw, ch, r, draw)
        img.alpha_composite(card, (M, M))
        return img

    def _key(self, dst, text, x, cy):
        """Tasto: bordo 2 px, angoli 5, ombra piena 2/2. Torna la larghezza (px fisici)."""
        f = self.key_font()
        tw = label_width(text, f)
        w = int(round(tw + self.px(8) * 2 + self.px(2) * 2))
        h = self.ipx(24)
        y = int(round(cy - h / 2.0))
        r = self.ipx(5)
        o = self.ipx(2)
        bw = max(1, self.ipx(2))
        blit(dst, solid((w, h), INK, rr_mask(w, h, r)), x + o, y + o)
        blit(dst, solid((w, h), INK, rr_mask(w, h, r)), x, y)
        blit(dst, solid((w - 2 * bw, h - 2 * bw), (255, 255, 255), rr_mask(w - 2 * bw, h - 2 * bw, max(0, r - bw))),
             x + bw, y + bw)
        text_center(dst, text, f, INK, x + (w - tw) / 2.0, cy)
        return w

    def _footer(self, inner, mode, fy, fh):
        """Pie' con i tasti veri di Wavetype (wavetype.py: Win+Ctrl ferma e incolla, Esc annulla,
        Win+Ctrl+R recupera l'ultima registrazione)."""
        if mode[:2] == "e_":
            return self._efooter(inner, mode, fy, fh)
        f = self.ft_font()
        cy = fy + fh / 2.0
        x0 = self.px(18) - self.b()
        xr = inner.width - (self.px(12) - self.b())
        gap = self.px(7)
        if mode == "paused":
            x = x0
            x += self._key(inner, "Win+Ctrl", int(x), cy) + gap
            text_center(inner, "insert", f, INK, x, cy)
            tw = label_width("cancel", f)
            kw = self._keyw("Esc")
            x = xr - tw - gap - kw
            self._key(inner, "Esc", int(x), cy)
            text_center(inner, "cancel", f, INK, x + kw + gap, cy)
        elif mode == "cancelled":
            text_center(inner, "Nothing inserted", f, INK, x0, cy)
            tw = label_width("bring it back", f)
            kw = self._keyw("Win+Ctrl+R")
            x = xr - tw - gap - kw
            self._key(inner, "Win+Ctrl+R", int(x), cy)
            text_center(inner, "bring it back", f, INK, x + kw + gap, cy)
        elif mode == "offline":
            text_center(inner, "Preview off, still recording", f, INK, x0, cy)
            text_center(inner, "Text lands on stop", f, INK, xr, cy, right=True)
        elif mode in R_FOOTERS:
            left, key, right = R_FOOTERS[mode]
            text_center(inner, left, f, INK, x0, cy)
            tw = label_width(right, f)
            kw = self._keyw(key)
            x = xr - tw - gap - kw
            self._key(inner, key, int(x), cy)
            text_center(inner, right, f, INK, x + kw + gap, cy)

    def _keyw(self, text):
        return int(round(label_width(text, self.key_font()) + self.px(8) * 2 + self.px(2) * 2))

    # -------- parti vive (ogni frame)
    def front(self, panel, img, ox, oy, mode, cw, ch, above, m, now):
        b = self.b()
        hd = self.px(self.HD)
        cy = oy + b + hd / 2.0
        x = ox + b + self.px(14)
        lab, dot = self._label(mode, panel)
        if dot:
            fill = (255, 255, 255) if mode == "paused" and not panel.preview_name else A_CUT
            put_center(img, self._dot(fill), x + self.px(5.5), cy)
            x += self.px(11 + 7)
        f = self.lab_font()
        tr = self.px(13 * 0.02)
        x += text_center(img, lab, f, INK, x, cy, tr)
        cnt = panel.count_text()
        if cnt and mode == "live":
            x += text_center(img, " · " + cnt.upper(), f, INK, x, cy, tr, alpha=0.55)
        if mode == "recovering":                    # durata dell'audio accanto al titolo
            x += text_center(img, " · " + panel.timer_text(), f, INK, x, cy, tr, alpha=0.55)
        if mode in ("listening", "live", "offline", "paused"):
            self._bars(img, x + self.px(10 + 4), cy, panel.levels(9), mode == "paused")
        if mode not in NO_TIMER and mode != "recovering" and not (mode == "listening" and m < 0.5):
            self._timer(img, ox + cw - b - self.px(10), cy, panel.timer_text(), A_TAB.get(mode, "#FFD83D"),
                        1.0 if mode != "listening" else m)
        if mode == "formatting" and ch > b * 2 + hd + b:
            self._progress(img, ox, oy + ch, cw, panel.progress())

    def _dot(self, fill):
        key = ("a-dot", tuple(fill), self.k)
        sp = _MASKS.get(key)
        if sp is None:
            S = 4
            d = self.px(11)
            side = int(math.ceil(d)) + 2
            big = Image.new("RGBA", (side * S, side * S), (0, 0, 0, 0))
            c = side * S / 2.0
            rr = d * S / 2.0
            bw = self.px(2) * S
            dd = ImageDraw.Draw(big)
            dd.ellipse([c - rr, c - rr, c + rr, c + rr], fill=INK + (255,))
            dd.ellipse([c - rr + bw, c - rr + bw, c + rr - bw, c + rr - bw], fill=tuple(fill) + (255,))
            sp = _MASKS[key] = big.resize((side, side), Image.LANCZOS)
        return sp

    def _bars(self, img, x, cy, lv, flat):
        d = ImageDraw.Draw(img)
        bw = self.px(4)
        gap = self.px(3)
        for i, v in enumerate(lv):
            h = self.px(4) if flat else max(self.px(4), round(v * 22) * self.k)
            x0 = x + i * (bw + gap)
            d.rounded_rectangle([round(x0), round(cy - h / 2), round(x0 + bw) - 1, round(cy + h / 2) - 1],
                                radius=max(0, int(self.k)), fill=INK + (255,))

    def _timer(self, img, xr, cy, t, tab, alpha):
        f = self.tm_font()
        tw = label_width(t, f)
        w = int(round(tw + self.px(7) * 2))
        h = int(round(self.px(12.5) + self.px(5) * 2))
        key = ("a-tm", w, h)
        bg = _MASKS.get(key)
        if bg is None:
            bg = _MASKS[key] = solid((w, h), INK, rr_mask(w, h, self.ipx(4)))
        x = int(round(xr - w))
        y = int(round(cy - h / 2.0))
        if alpha < 0.99:
            bg = solid((w, h), INK, rr_mask(w, h, self.ipx(4)), alpha)
        blit(img, bg, x, y)
        text_center(img, t, f, hexc(tab), x + (w - tw) / 2.0, cy, alpha=alpha)

    def _progress(self, img, ox, bottom, cw, p):
        """Barra a strisce: bordo 2, riempimento a righe oblique nero/lilla, filo a destra."""
        b = self.b()
        x0 = ox + self.ipx(18) + b
        x1 = ox + cw - self.ipx(18) - b
        h = self.ipx(12)
        y1 = bottom - b - self.ipx(16)
        y0 = y1 - h
        w = x1 - x0
        key = ("a-prog", w, h)
        base = _MASKS.get(key)
        if base is None:
            bw = max(1, self.ipx(2))
            stripes = Image.new("RGBA", (w, h), A_FORMAT + (255,))
            sd = ImageDraw.Draw(stripes)
            period = self.px(8) * math.sqrt(2)
            ink_w = self.px(3) * math.sqrt(2)
            xx = -h
            while xx < w + h:
                sd.polygon([(xx, h), (xx + h, 0), (xx + h + ink_w, 0), (xx + ink_w, h)], fill=INK + (255,))
                xx += period
            white = Image.new("RGBA", (w, h), (255, 255, 255, 255))
            frame = solid((w, h), INK, rr_mask(w, h, self.ipx(3)))
            _MASKS[key] = base = (stripes, white, frame, bw)
        stripes, white, frame, bw = base
        fill_w = int(round((w - 2 * bw) * max(0.0, min(1.0, p))))
        inner = white.crop((0, 0, w - 2 * bw, h - 2 * bw)).copy()
        if fill_w > 0:
            inner.paste(stripes.crop((0, 0, fill_w, h - 2 * bw)), (0, 0))
            ImageDraw.Draw(inner).rectangle([fill_w, 0, fill_w + bw - 1, h], fill=INK + (255,))
        inner.putalpha(rr_mask(w - 2 * bw, h - 2 * bw, max(0, self.ipx(3) - bw)))
        card = frame.copy()
        card.alpha_composite(inner, (bw, bw))
        blit(img, card, x0, y0)

    # -------- parole
    def highlight(self, layer, x, y, w, h, alpha):
        """Evidenziatore lime sulla parola appena fissata (box-shadow 0 0 0 2px, angoli 2).
        Alto come l'area del testo in Chrome (1,21 em, misurato sul mockup: 37 px a 150%)."""
        s = self.px(2)
        h2 = self.px(17 * 1.21)
        y = y + h / 2.0 - h2 / 2.0
        h = h2
        W, H = int(round(w + 2 * s)), int(round(h + 2 * s))
        blit(layer, solid((W, H), A_FIXED, rr_mask(W, H, self.ipx(2)), alpha), x - s, y - s)

    def strike(self, layer, x, line_y, w, p, alpha):
        """Barra rossa spessa 3 px, ruotata di -4 gradi, 3 px oltre la parola da ogni lato."""
        ext = self.px(3)
        L = (w + 2 * ext) * p
        if L < 1:
            return
        th = self.px(3)
        ang = math.radians(4)
        key = ("a-strike", int(L), round(alpha, 2), self.k)
        sp = _MASKS.get(key)
        if sp is None:
            S = 3
            full_w = w + 2 * ext
            hh = full_w * math.sin(ang) + th * 2 + 4
            Wb, Hb = int(L + 4), int(hh + 2)
            big = Image.new("L", (Wb * S, Hb * S), 0)
            cyb = Hb / 2.0
            cxm = full_w / 2.0                     # rotazione attorno al centro della parola intera
            pts = []
            for (px_, py_) in ((0, -th / 2), (L, -th / 2), (L, th / 2), (0, th / 2)):
                dx = px_ - cxm
                rx = dx * math.cos(ang) + py_ * math.sin(ang)
                ry = -dx * math.sin(ang) + py_ * math.cos(ang)
                pts.append(((rx + cxm + 2) * S, (ry + cyb) * S))
            ImageDraw.Draw(big).polygon(pts, fill=255)
            m = big.resize((Wb, Hb), Image.LANCZOS)
            sp = solid((Wb, Hb), A_CUT, m, alpha)
            if len(_MASKS) > 600:
                _MASKS.clear()
            _MASKS[key] = sp
        blit(layer, sp, x - ext - 2, line_y + self.px(self.line_h) / 2.0 + th / 2.0 - sp.height / 2.0)

    def cancel_line(self, layer, x, cy, w, alpha):
        """text-decoration: line-through 2.5px rosso (stato annullato)."""
        h = max(1, self.ipx(2.5))
        blit(layer, solid((max(1, int(round(w))), h), A_CUT, Image.new("L", (max(1, int(round(w))), h), 255), alpha),
             x, cy - h / 2.0)

    # -------- Edit Mode
    # La testata diventa azzurra (#7FDBFF): a colpo d'occhio non e' la dettatura (gialla).
    E_TAB = {"e_listening": "#7FDBFF", "e_rewriting": "#7FDBFF", "e_done": "#7FDBFF",
             "e_offline": "#FFB23F", "e_cancelled": "#FF6B4A", "e_nothing": "#EDE8DA"}
    E_LAB = {"e_listening": ("EDIT", True), "e_rewriting": ("REWRITING", True),
             "e_offline": ("EDIT", True), "e_cancelled": ("CANCELLED", False),
             "e_nothing": ("NOTHING SELECTED", False)}
    edit_line_h = 22
    edit_inset = 2.5 + 16        # bordo + padding di .said/.chips
    edit_pad_r = 2.5 + 16
    ec_commit = hexc("#3A372F")
    ec_tent = hexc("#8C887B")
    ec_gone = hexc("#9A968A")
    ec_ph = hexc("#8C887B")
    ec_you = hexc("#8C887B")

    def tab_for(self, mode):
        return self.E_TAB.get(mode) if mode[:2] == "e_" else A_TAB.get(mode, "#FFD83D")

    def edit_body_font(self):
        return font("bricolage", self.px(15), wght=600, opsz=15)

    def edit_you_font(self):
        return font("jbmono", self.px(11), wght=700)

    def edit_you_tr(self):
        return self.px(11 * 0.06)

    def edit_chip_font(self):
        return font("bricolage", self.px(13.5), wght=700, opsz=13.5)

    def edit_kbd_font(self):
        return font("jbmono", self.px(11), wght=700)

    def edit_foot_h(self):
        return self.B + 10 + 24 + 10             # filo + pie' (come la dettatura)

    def edit_zones(self, mode, above, m=1.0, chip=True):
        top = self.B + self.HD + self.B
        if mode in E_SAID or mode == "e_done":
            return top, self.edit_chips_h(), 12, self.B
        bot = self.B
        has_chip = mode in E_CHIPS_ONE and chip
        if has_chip:
            bot += self.edit_chips_h()
        if mode == "e_rewriting":
            bot += 12 + 16 + (0 if has_chip else 14)   # .prog: alta 12, margine sotto 16
        if mode in E_FOOT:
            bot += self.edit_foot_h()
        return top, 0, 0, bot

    def edit_max_height(self):
        t, pt, pb, b = self.edit_zones("e_listening", True)
        a = t + pt + 2 * self.edit_line_h + pb + b
        t, _pt, _pb, b = self.edit_zones("e_offline", True)
        return max(a, t + b)

    def _chip(self, img, x, y, w, h, chip, state, full, tab=None):
        """Un chip: bordo 2 px, angoli 7, numero in un quadratino nero. Acceso = fondo del colore
        della testata; spento = 35% (l'altro comando resta leggibile, non sparisce)."""
        tab = tab or self.E_TAB["e_listening"]
        W, H = max(2, int(round(w))), max(2, int(round(h)))
        key = ("s-chip", self.k, W, H, chip.id, state, full, tab)
        sp = _MASKS.get(key)
        if sp is None:
            o = self.ipx(3)
            r, bw = self.ipx(7), max(1, self.ipx(2))
            sp = Image.new("RGBA", (W + o, H + o), (0, 0, 0, 0))
            if state == "on":
                sp.alpha_composite(solid((W, H), INK, rr_mask(W, H, r)), (o, o))
            sp.alpha_composite(solid((W, H), INK, rr_mask(W, H, r)), (0, 0))
            fill = hexc(tab) if state == "on" else (255, 255, 255)
            sp.alpha_composite(solid((W - 2 * bw, H - 2 * bw), fill,
                                     rr_mask(W - 2 * bw, H - 2 * bw, max(0, r - bw))), (bw, bw))
            kw, kh = self.edit_kbd_w(), self.px(18)
            tw = self.chip_text_w(chip.label) if full else 0.0
            inner = kw + (self.px(self.E_CHIP_IN) + tw if full else 0.0)
            cx, cy = (W - inner) / 2.0, H / 2.0
            kf = self.edit_kbd_font()
            blit(sp, solid((int(round(kw)), int(round(kh))), INK,
                           rr_mask(int(round(kw)), int(round(kh)), self.ipx(4))), cx, cy - kh / 2.0)
            text_center(sp, chip.key, kf, (255, 255, 255),
                        cx + (kw - label_width(chip.key, kf)) / 2.0, cy)
            if full:
                text_center(sp, chip.label, self.edit_chip_font(), INK,
                            cx + kw + self.px(self.E_CHIP_IN), cy)
            if state == "off":
                sp.putalpha(sp.getchannel("A").point(lambda v: int(v * 0.35)))
            if len(_MASKS) > 600:
                _MASKS.clear()
            _MASKS[key] = sp
        blit(img, sp, x, y)

    def _efooter(self, inner, mode, fy, fh):
        f = self.ft_font()
        cy = fy + fh / 2.0
        x0 = self.px(18) - self.b()
        xr = inner.width - (self.px(12) - self.b())
        gap = self.px(7)
        left, key, right = E_FOOTERS[mode]
        text_center(inner, left, f, INK, x0, cy)
        if key:
            tw = label_width(right, f)
            kw = self._keyw(key)
            x = xr - tw - gap - kw
            self._key(inner, key, int(x), cy)
            text_center(inner, right, f, INK, x + kw + gap, cy)

    def edit_front(self, panel, img, ox, oy, mode, cw, ch, above, m, now):
        b = self.b()
        top, pt, _pb, _bot = (v * self.k for v in self.edit_zones(mode, above, m))
        cy = oy + b + self.px(self.HD) / 2.0
        x = ox + b + self.px(14)
        lab, icon = self.E_LAB.get(mode, ("EDIT", True))
        if icon:
            blit(img, sel_mark(self.k, 15, 13, INK, hexc(self.tab_for(mode))), x, cy - self.px(13) / 2.0)
            x += self.px(15 + 10)
        f = self.lab_font()
        x += text_center(img, lab, f, INK, x, cy, self.px(13 * 0.02))
        sub = panel.edit_sub()
        if sub:
            x += self.px(7) + text_center(img, sub, self.ft_font(), INK, x + self.px(7), cy, alpha=0.75)
        if mode == "e_listening":
            self._bars(img, x + self.px(10 + 4), cy, panel.levels(9), False)
        self._edit_body(panel, img, ox, oy, mode, cw, ch, top, pt)
        if mode == "e_rewriting":
            self._progress(img, ox, oy + ch, cw, panel.progress())

    def _edit_body(self, panel, img, ox, oy, mode, cw, ch, top, pt):
        """Riga dei chip ed etichetta YOU: uguali nei tre stili, cambiano solo le misure."""
        x0 = ox + self.px(self.edit_inset)
        y = oy + top + self.px(self.E_CHIP_PAD)
        chips, lit, dim, only = panel.edit_row()
        if chips or only is not None:
            tab = self.tab_for(mode) if hasattr(self, "tab_for") else None
            self.edit_chip_row(img, x0, y, cw - 2 * self.px(self.edit_inset),
                               chips, lit, dim, only, tab)
        if mode in E_SAID:
            lh = self.px(self.edit_line_h)
            ycy = oy + top + pt + lh / 2.0
            text_center(img, E_YOU.upper(), self.edit_you_font(), self.ec_you, x0, ycy, self.edit_you_tr())
            if not panel.has_words():
                text_center(img, E_HINT, self.edit_body_font(), self.ec_ph, ox + panel.text_x, ycy)

    def edit_pill(self, panel):
        """Pillola di esito: "✓ GRAMMAR FIXED · 26 words · CTRL+Z undo"."""
        f = self.lab_font()
        fe = font("jbmono", self.px(12), wght=600)
        fk = font("bricolage", self.px(12.5), wght=600, opsz=12.5)
        t1, t2 = panel.edit_done_text(), panel.edit_words_text()
        chk = self.px(10)
        gap = self.px(9)
        kw = self._keyw("Ctrl+Z")
        w = int(round(self.px(14) + chk + self.px(7) + label_width(t1, f) + gap + label_width(t2, fe)
                      + gap + kw + self.px(4) + label_width("undo", fk) + self.px(8) + 2 * self.b()))
        h = self.ipx(38)
        r, b = self.ipx(10), self.b()
        M = panel.margin
        img = Image.new("RGBA", (w + 2 * M, h + 2 * M), (0, 0, 0, 0))
        ri = int(round(self.px(1.5)))
        img.alpha_composite(solid((w + 2 * ri, h + 2 * ri), (255, 255, 255),
                                  rr_mask(w + 2 * ri, h + 2 * ri, r + ri), 0.85), (M - ri, M - ri))
        img.alpha_composite(solid((w, h), INK, rr_mask(w, h, r)), (M + self.ipx(4), M + self.ipx(4)))
        img.alpha_composite(solid((w, h), INK, rr_mask(w, h, r)), (M, M))
        img.alpha_composite(solid((w - 2 * b, h - 2 * b), hexc(self.E_TAB["e_done"]),
                                  rr_mask(w - 2 * b, h - 2 * b, r - b)), (M + b, M + b))
        cy = M + h / 2.0
        x = M + b + self.px(14)
        self._check(img, x, cy, chk)
        x += chk + self.px(7)
        x += text_center(img, t1, f, INK, x, cy) + gap
        x += text_center(img, t2, fe, INK, x, cy + self.px(0.5), alpha=0.75) + gap
        self._key(img, "Ctrl+Z", int(x), cy)
        text_center(img, "undo", fk, INK, x + kw + self.px(4), cy)
        return img

    # -------- pillola "Inserted"
    def pill(self, panel):
        k = self.k
        f = self.lab_font()
        fe = font("jbmono", self.px(12), wght=600)
        n = panel.words_total()
        t1 = "INSERTED"
        t2 = f"{n} word" + ("" if n == 1 else "s")
        chk = self.px(10)
        w = int(round(self.px(14) * 2 + chk + self.px(3) + label_width(t1, f) + self.px(8)
                      + label_width(t2, fe) + 2 * self.b()))
        h = self.ipx(38)
        r = self.ipx(10)
        b = self.b()
        M = panel.margin
        img = Image.new("RGBA", (w + 2 * M, h + 2 * M), (0, 0, 0, 0))
        ri = int(round(self.px(1.5)))
        img.alpha_composite(solid((w + 2 * ri, h + 2 * ri), (255, 255, 255), rr_mask(w + 2 * ri, h + 2 * ri, r + ri), 0.85),
                            (M - ri, M - ri))
        img.alpha_composite(solid((w, h), INK, rr_mask(w, h, r)), (M + self.ipx(4), M + self.ipx(4)))
        img.alpha_composite(solid((w, h), INK, rr_mask(w, h, r)), (M, M))
        img.alpha_composite(solid((w - 2 * b, h - 2 * b), A_FIXED, rr_mask(w - 2 * b, h - 2 * b, r - b)), (M + b, M + b))
        cy = M + h / 2.0
        x = M + b + self.px(14)
        self._check(img, x, cy, chk)
        x += chk + self.px(3)
        x += text_center(img, t1, f, INK, x, cy) + self.px(8)
        text_center(img, t2, fe, INK, x, cy + self.px(0.5), alpha=0.7)
        return img

    def _check(self, img, x, cy, s):
        """Il font non ha il segno di spunta: lo si disegna (stesso tratto del testo, 800)."""
        S = 4
        W, H = int(s) + 2, int(s) + 2
        big = Image.new("L", (W * S, H * S), 0)
        lw = max(1, int(self.px(2.2) * S))
        pts = [(0.08 * s * S, 0.55 * s * S), (0.38 * s * S, 0.85 * s * S), (0.95 * s * S, 0.18 * s * S)]
        ImageDraw.Draw(big).line(pts, fill=255, width=lw, joint="curve")
        m = big.resize((W, H), Image.LANCZOS)
        blit(img, solid((W, H), INK, m), x, cy - H / 2.0)


# ================================================================= B · GLYPH
B_RED = hexc("#D71921")
B_LINE = hexc("#262626")
B_GREY = hexc("#8a8a8a")


class Glyph(Style):
    name = "glyph"
    shadow_reach = 10 + 3 * 17      # spread + 3 sigma (px CSS): fin dove l'ombra sfuma
    shadow_dy = 14
    margin = 48
    over_mask = ((0, 0.0), (30, 1.0))
    radius = 18
    HD = 46
    text_inset = 20
    pad_r = 20
    c_commit = (255, 255, 255)
    c_tent = hexc("#7c7c7c")
    c_gone = hexc("#a8a8a8")
    c_cancel = B_GREY           # annullato: grigio nel sans (il dot-matrix lungo non si leggeva)

    def h_period(self):
        return 2 * max(2, self.ipx(3))           # passo del filo tratteggiato sotto la testata

    def body_font(self):
        return font("sgrotesk", self.px(16.5), wght=400)

    def struck_font(self):
        return font("doto", self.px(17.5), wght=900)

    def struck_tracking(self):
        return self.px(17.5 * 0.02)

    def lab_font(self):
        return font("smono-b", self.px(10.5))

    def ft_font(self):
        return font("smono", self.px(10.5))

    def tm_font(self):
        return font("doto", self.px(23), wght=900)

    def hd_h(self, m):
        return 40 + (self.HD - 40) * m

    def zones(self, mode, above, m=1.0):
        top = self.hd_h(m)
        bot = 0
        if self.has_foot(mode):
            bot = 20 + 14
        elif mode == "formatting":
            bot = 4 + 16
        return top, 13, 16, bot

    def max_height(self):
        return self.HD + 13 + 3 * self.line_h + 16 + 34

    def compact_size(self, panel, mode):
        if mode == "recovering":            # niente matrice del livello: la pillola si stringe
            segs, _ = self._label(mode, panel)
            f = self.lab_font()
            tr = self.px(10.5 * 0.14)
            lw = sum(label_width(t, f, tr) + tr for t, _ in segs) / self.k
            return 16 + 8 + 8 + lw + 18, 40
        return 300, 40

    def _label(self, mode, panel):
        """[(testo, colore)], tipo di pallino."""
        if panel.preview_name:
            return [(panel.preview_name.upper(), (255, 255, 255))], "red"
        cnt = panel.count_text()
        base = {"listening": ("LISTENING", "red"), "live": ("REC", "red"), "paused": ("PAUSED", "red"),
                "formatting": ("FORMATTING", "white"), "cancelled": ("CANCELLED", "red"),
                "offline": ("REC", "red"), "recovering": ("RECOVERING", "white"),
                "recovered": ("RECOVERED", "white"), "no_audio": ("NOTHING YET", "white"),
                "unrecovered": ("OFFLINE", "white")}.get(mode, ("REC", "red"))
        segs = [(base[0], (255, 255, 255))]
        if mode == "offline":
            segs.append((" · OFFLINE", B_GREY))
        elif mode == "recovering":
            segs.append((" · " + panel.timer_text(), B_GREY))
        elif cnt and mode == "live":
            segs.append((" · " + cnt.upper(), B_GREY))
        return segs, base[1]

    def plate(self, panel, mode, cw, ch, above, m, tex=True):
        r = self.ipx(14 + (self.radius - 14) * m)
        card = Image.new("RGBA", (cw, ch), (0, 0, 0, 255))
        if tex:
            card.alpha_composite(self._texture(cw, ch))
        hd = self.ipx(self.hd_h(m))
        if ch > hd + self.px(4):                                  # filo tratteggiato sotto la testata
            d = ImageDraw.Draw(card)
            dash = max(2, self.ipx(3))
            x = 0
            y = hd - max(1, self.ipx(1))
            while x < cw:
                d.rectangle([x, y, x + dash - 1, y + max(1, self.ipx(1)) - 1], fill=B_LINE + (255,))
                x += 2 * dash
            if self.has_foot(mode):
                self._footer(card, mode, ch - self.px(34), self.px(20))
        card.putalpha(rr_mask(cw, ch, r))
        M = panel.margin

        def draw(img, cw, ch):
            self._shadows(img, M, cw, ch, r)
            g = max(1, self.ipx(1))
            img.alpha_composite(solid((cw + 2 * g, ch + 2 * g), B_LINE, rr_mask(cw + 2 * g, ch + 2 * g, r + g)),
                                (M - g, M - g))
        img = self.backdrop(M, cw, ch, r, draw)
        img.alpha_composite(card, (M, M))
        return img

    def _shadows(self, img, M, cw, ch, r):
        # 0 14px 34px -10px rgba(0,0,0,.45) + 0 2px 6px rgba(0,0,0,.25)
        sp, dy1, dy2 = self.px(10), self.px(14), self.px(2)
        img.alpha_composite(shadow_image(img.width, img.height, [
            (M + sp, M + sp + dy1, M + cw - sp, M + ch - sp + dy1, self.px(34) / 2.0, 0.45),
            (M, M + dy2, M + cw, M + ch + dy2, self.px(6) / 2.0, 0.25)]))

    def texture_layer(self, cw, ch, m):
        """La texture a punti gia' ritagliata sulla forma della card: live_panel la posa sopra
        una lastra ricomposta a fette (cosi' la fase dei punti resta ancorata in alto)."""
        key = ("b-texl", cw, ch, m, self.k)
        t = _MASKS.get(key)
        if t is None:
            t = self._texture(cw, ch).copy()
            a = np.asarray(t.getchannel("A")).astype(np.uint16)
            mk = np.asarray(rr_mask(cw, ch, self.ipx(14 + (self.radius - 14) * m))).astype(np.uint16)
            t.putalpha(Image.fromarray((a * mk // 255).astype(np.uint8), "L"))
            if len(_MASKS) > 600:
                _MASKS.clear()
            _MASKS[key] = t
        return t

    def _texture(self, cw, ch):
        """radial-gradient(circle, rgba(255,255,255,.075) 0.9px, transparent 1.1px) ogni 8 px."""
        key = ("b-tex", cw, ch, self.k)
        t = _MASKS.get(key)
        if t is None:
            step = self.px(8)
            S = 4
            n = int(round(step * S))
            tile = Image.new("L", (n, n), 0)
            c = n / 2.0
            r = self.px(1.0) * S
            ImageDraw.Draw(tile).ellipse([c - r, c - r, c + r, c + r], fill=int(255 * 0.075))
            n1 = max(1, int(round(step)))
            tile = tile.resize((n1, n1), Image.LANCZOS)
            arr = np.asarray(tile)
            reps = (ch // arr.shape[0] + 2, cw // arr.shape[1] + 2)
            big = np.tile(arr, reps)[:ch, :cw]
            t = solid((cw, ch), (255, 255, 255), Image.fromarray(np.ascontiguousarray(big), "L"))
            _MASKS[key] = t
        return t

    def _key(self, dst, text, x, cy):
        f = self.ft_font()
        tr = self.px(10.5 * 0.12)
        tw = label_width(text, f, tr)
        w = int(round(tw + self.px(7) * 2 + tr))
        h = self.ipx(20)
        y = int(round(cy - h / 2.0))
        bw = max(1, self.ipx(1))
        r = self.ipx(5)
        blit(dst, solid((w, h), hexc("#3a3a3a"), rr_mask(w, h, r)), x, y)
        blit(dst, solid((w - 2 * bw, h - 2 * bw), (0, 0, 0), rr_mask(w - 2 * bw, h - 2 * bw, r - bw)), x + bw, y + bw)
        text_center(dst, text, f, (255, 255, 255), x + self.px(7), cy, tr)
        return w

    def _keyw(self, text):
        tr = self.px(10.5 * 0.12)
        return int(round(label_width(text, self.ft_font(), tr) + self.px(7) * 2 + tr))

    def _footer(self, card, mode, fy, fh):
        if mode[:2] == "e_":
            return self._efooter(card, mode, fy, fh)
        f = self.ft_font()
        tr = self.px(10.5 * 0.12)
        cy = fy + fh / 2.0
        x0 = self.px(20)
        xr = card.width - self.px(16)
        gap = self.px(6)
        if mode == "paused":
            x = x0 + self._key(card, "WIN+CTRL", int(x0), cy) + gap
            text_center(card, "INSERT", f, B_GREY, x, cy, tr)
            tw = label_width("CANCEL", f, tr)
            kw = self._keyw("ESC")
            x = xr - tw - gap - kw
            self._key(card, "ESC", int(x), cy)
            text_center(card, "CANCEL", f, B_GREY, x + kw + gap, cy, tr)
        elif mode == "cancelled":
            text_center(card, "NOTHING INSERTED", f, B_GREY, x0, cy, tr)
            tw = label_width("BRINGS IT BACK", f, tr)
            kw = self._keyw("WIN+CTRL+R")
            x = xr - tw - gap - kw
            self._key(card, "WIN+CTRL+R", int(x), cy)
            text_center(card, "BRINGS IT BACK", f, B_GREY, x + kw + gap, cy, tr)
        elif mode == "offline":
            text_center(card, "PREVIEW OFF · STILL RECORDING", f, B_GREY, x0, cy, tr)
            text_center(card, "TEXT LANDS WHEN YOU STOP", f, B_GREY, xr, cy, tr, right=True)
        elif mode in R_FOOTERS:
            left, key, right = R_FOOTERS[mode]
            text_center(card, left.upper(), f, B_GREY, x0, cy, tr)
            tw = label_width(right.upper(), f, tr)
            kw = self._keyw(key.upper())
            x = xr - tw - gap - kw
            self._key(card, key.upper(), int(x), cy)
            text_center(card, right.upper(), f, B_GREY, x + kw + gap, cy, tr)

    def front(self, panel, img, ox, oy, mode, cw, ch, above, m, now):
        hd = self.px(self.hd_h(m))
        cy = oy + hd / 2.0
        x = ox + self.px(16 + 4 * m)
        segs, dot = self._label(mode, panel)
        if dot == "red":
            put_center(img, disc(self.px(7), B_RED, 0.18), x + self.px(4), cy)       # anello 3 px
            put_center(img, disc(self.px(4), B_RED), x + self.px(4), cy)
        else:
            put_center(img, disc(self.px(4), (255, 255, 255)), x + self.px(4), cy)
        x += self.px(8 + 8)
        f = self.lab_font()
        tr = self.px(10.5 * 0.14)
        for t, col in segs:
            x += text_center(img, t, f, col, x, cy, tr) + tr
        xr = ox + cw - self.px(18 - 2 * m)
        show_tm = mode not in NO_TIMER and mode != "recovering" and m > 0.02
        if show_tm:
            tx = self._timer(img, xr, cy, panel.timer_text(pad=True), m)
            xr = tx - self.px(14)
        if mode in ("listening", "live", "paused", "offline"):
            self._vu(img, xr, cy, panel.levels(24), mode == "paused")
        if mode == "formatting":
            self._bar(img, ox + self.px(20), oy + ch - self.px(16) - self.px(4), cw - self.px(40), panel.progress())

    def _timer(self, img, xr, cy, t, alpha):
        """Doto 900 23 px; i due punti sono una colonnina di due puntini (come il mockup)."""
        f = self.tm_font()
        a, b = t.split(":")
        wa = label_width(a, f)
        wb = label_width(b, f)
        colw = self.px(3 + 6)
        total = max(self.px(52), wa + colw + wb)
        x = xr - total
        cyt = cy + self.px(0.5)
        text_center(img, a, f, (255, 255, 255), x, cyt, alpha=alpha)
        cx = x + wa + self.px(3) + self.px(1.5)
        for dy in (-3.25, 3.25):
            put_center(img, disc(self.px(1.4), (255, 255, 255), alpha), cx, cy + self.px(dy) - self.px(0.5))
        text_center(img, b, f, (255, 255, 255), x + wa + colw, cyt, alpha=alpha)
        return x

    def _vu(self, img, xr, cy, lv, paused):
        """Matrice 24 x 6 di punti: storia del livello voce, la piu' recente a destra."""
        cols, rows = 24, 6
        p = self.px(4.6)
        W, H = cols * p, rows * p
        key = ("b-dots", self.k)
        dots = _MASKS.get(key)
        if dots is None:
            r = self.px(1.55)
            S = 4
            side = int(math.ceil(p))
            big = Image.new("L", (side * S, side * S), 0)
            c = side * S / 2.0
            ImageDraw.Draw(big).ellipse([c - r * S, c - r * S, c + r * S, c + r * S], fill=255)
            dm = np.asarray(big.resize((side, side), Image.LANCZOS)).astype(np.float32) / 255.0
            _MASKS[key] = dots = dm
        side = dots.shape[0]
        Wi, Hi = int(math.ceil(W)) + side, int(math.ceil(H)) + side
        gray = np.zeros((Hi, Wi), np.float32)
        alpha = np.zeros((Hi, Wi), np.float32)
        n = len(lv)
        for c in range(cols):
            v = lv[n - cols + c] if n - cols + c >= 0 else 0.0
            on = 0 if paused else int(round(math.sqrt(max(0.0, v)) * rows))
            fade = 0.35 + 0.65 * (c / cols)
            x0 = int(round(c * p))
            for k_ in range(rows):
                lit = k_ < on or k_ == 0
                if lit:
                    g = 255.0 * (0.28 * fade if (k_ == 0 and on == 0) else fade)
                else:
                    g = 30.0                                             # #1e1e1e
                y0 = int(round(H - (k_ + 1) * p))
                sl = (slice(y0, y0 + side), slice(x0, x0 + side))
                gray[sl] = np.maximum(gray[sl], g * dots)
                alpha[sl] = np.maximum(alpha[sl], dots)
        # colore = grigio su nero: rgb premoltiplicato -> diviso per alpha
        a8 = np.clip(alpha * 255, 0, 255).astype(np.uint8)
        with np.errstate(divide="ignore", invalid="ignore"):
            col = np.where(alpha > 0.004, gray / np.maximum(alpha, 1e-3), 0)
        c8 = np.clip(col, 0, 255).astype(np.uint8)
        sp = Image.fromarray(np.dstack([c8, c8, c8, a8]), "RGBA")
        blit(img, sp, xr - W, cy - H / 2.0)

    def _bar(self, img, x, y, w, p):
        """Glyph Progress: 4 px, fondo #1c1c1c, riempimento bianco con alone."""
        h = self.ipx(4)
        w = int(round(w))
        key = ("b-bar", w, h)
        bg = _MASKS.get(key)
        if bg is None:
            bg = _MASKS[key] = solid((w, h), hexc("#1c1c1c"), rr_mask(w, h, self.ipx(2)))
        blit(img, bg, x, y)
        fw = int(round(w * max(0.0, min(1.0, p))))
        if fw >= 2:
            key = ("b-fill", fw, h)
            sp = _MASKS.get(key)
            if sp is None:
                pad = self.ipx(12)
                m = Image.new("L", (fw + 2 * pad, h + 2 * pad), 0)
                ImageDraw.Draw(m).rounded_rectangle([pad - self.px(1), pad - self.px(1), pad + fw + self.px(1), pad + h + self.px(1)],
                                                    radius=self.ipx(3), fill=255)
                glow = m.filter(ImageFilter.GaussianBlur(self.px(10) / 2.0)).point(lambda v: int(v * 0.55))
                sp = solid(glow.size, (255, 255, 255), glow)
                sp.alpha_composite(solid((fw, h), (255, 255, 255), rr_mask(fw, h, self.ipx(2))), (pad, pad))
                if len(_MASKS) > 600:
                    _MASKS.clear()
                _MASKS[key] = sp
            blit(img, sp, x - self.ipx(12), y - self.ipx(12))

    def strike(self, layer, x, line_y, w, p, alpha):
        """Linea rossa 2 px al 53% della riga, 2 px oltre la parola, estremi arrotondati."""
        ext = self.px(2)
        L = (w + 2 * ext) * p
        if L < 1:
            return
        h = max(1, self.ipx(2))
        key = ("b-strike", int(L), h, round(alpha, 2))
        sp = _MASKS.get(key)
        if sp is None:
            sp = _MASKS[key] = solid((int(L), h), B_RED, rr_mask(int(L), h, h // 2), alpha)
        blit(layer, sp, x - ext, line_y + self.px(self.line_h) * 0.53 - h / 2.0)

    # -------- Edit Mode
    # Il segno di Edit e' la targhetta bianca "EDIT" nella testata (la dettatura ha il pallino rosso).
    E_LAB = {"e_listening": ("EDIT", "tag"), "e_rewriting": ("REWRITING", "tag"),
             "e_offline": ("EDIT", "tag"), "e_cancelled": ("CANCELLED", "dot"),
             "e_nothing": ("NOTHING SELECTED", "")}
    E_TAGBG = {"e_offline": B_GREY}
    edit_line_h = 22
    edit_inset = 20
    edit_pad_r = 20
    E_CHIP_PAD = 14
    E_CHIP_BOT = 16
    E_CHIP_SIDE = 10
    E_KBD_W = 12
    ec_commit = (255, 255, 255)
    ec_tent = hexc("#7c7c7c")
    ec_gone = hexc("#a8a8a8")
    ec_ph = hexc("#7c7c7c")
    ec_you = hexc("#7c7c7c")

    def edit_body_font(self):
        return font("sgrotesk", self.px(15), wght=400)

    def edit_you_font(self):
        return font("smono", self.px(10))

    def edit_you_tr(self):
        return self.px(10 * 0.14)

    def edit_chip_font(self):
        return font("smono", self.px(10.5))

    def edit_chip_tr(self):
        return self.px(10.5 * 0.06)

    def edit_kbd_font(self):
        return font("doto", self.px(15), wght=900)

    def edit_foot_h(self):
        return 20 + 14

    def edit_zones(self, mode, above, m=1.0, chip=True):
        top = self.HD
        if mode in E_SAID or mode == "e_done":
            return top, self.edit_chips_h(), 14, 0
        bot = 0
        has_chip = mode in E_CHIPS_ONE and chip
        if has_chip:
            bot += self.edit_chips_h()
        if mode == "e_rewriting":
            bot += 4 + 16 + (0 if has_chip else 14)    # .glyph: barra 4, margine sotto 16
        if mode in E_FOOT:
            bot += self.edit_foot_h() + (0 if has_chip else 14)
        return top, 0, 0, bot

    def edit_max_height(self):
        t, pt, pb, b = self.edit_zones("e_listening", True)
        a = t + pt + 2 * self.edit_line_h + pb + b
        t, _pt, _pb, b = self.edit_zones("e_offline", True)
        return max(a, t + b)

    def _chip(self, img, x, y, w, h, chip, state, full, tab=None):
        """Chip a pillola: filo grigio, numero in Doto. Acceso = fondo bianco, numero rosso."""
        W, H = max(2, int(round(w))), max(2, int(round(h)))
        key = ("g-chip", self.k, W, H, chip.id, state, full)
        sp = _MASKS.get(key)
        if sp is None:
            r, bw = H // 2, max(1, self.ipx(1))
            on = state == "on"
            sp = solid((W, H), (255, 255, 255) if on else hexc("#3a3a3a"), rr_mask(W, H, r))
            if not on:
                sp.alpha_composite(solid((W - 2 * bw, H - 2 * bw), (0, 0, 0),
                                         rr_mask(W - 2 * bw, H - 2 * bw, r - bw)), (bw, bw))
            kf, cf = self.edit_kbd_font(), self.edit_chip_font()
            tr = self.edit_chip_tr()
            kw = self.edit_kbd_w()
            tw = self.chip_text_w(chip.label) if full else 0.0
            inner = kw + (self.px(self.E_CHIP_IN) + tw if full else 0.0)
            cx, cy = (W - inner) / 2.0, H / 2.0
            text_center(sp, chip.key, kf, B_RED if on else B_GREY,
                        cx + (kw - label_width(chip.key, kf)) / 2.0, cy)
            if full:
                text_center(sp, chip.label.upper(), cf, (0, 0, 0) if on else (255, 255, 255),
                            cx + kw + self.px(self.E_CHIP_IN), cy, tr)
            if state == "off":
                sp.putalpha(sp.getchannel("A").point(lambda v: int(v * 0.35)))
            if len(_MASKS) > 600:
                _MASKS.clear()
            _MASKS[key] = sp
        blit(img, sp, x, y)

    def _efooter(self, card, mode, fy, fh):
        f = self.ft_font()
        tr = self.px(10.5 * 0.12)
        cy = fy + fh / 2.0
        x0, xr = self.px(20), card.width - self.px(16)
        gap = self.px(6)
        left, key, right = E_FOOTERS[mode]
        text_center(card, left.upper(), f, B_GREY, x0, cy, tr)
        if key:
            tw = label_width(right.upper(), f, tr)
            kw = self._keyw(key.upper())
            x = xr - tw - gap - kw
            self._key(card, key.upper(), int(x), cy)
            text_center(card, right.upper(), f, B_GREY, x + kw + gap, cy, tr)

    def edit_front(self, panel, img, ox, oy, mode, cw, ch, above, m, now):
        top, pt, _pb, _bot = (v * self.k for v in self.edit_zones(mode, above, m))
        cy = oy + self.px(self.HD) / 2.0
        x = ox + self.px(18)
        lab, kind = self.E_LAB.get(mode, ("EDIT", "tag"))
        f = self.lab_font()
        tr = self.px(10.5 * 0.14)
        if kind == "tag":
            x += self._tag(img, x, cy, lab, self.E_TAGBG.get(mode, (255, 255, 255))) + self.px(14)
        elif kind == "dot":
            put_center(img, disc(self.px(7), B_RED, 0.18), x + self.px(4), cy)
            put_center(img, disc(self.px(4), B_RED), x + self.px(4), cy)
            x += self.px(8 + 8)
            x += text_center(img, lab, f, (255, 255, 255), x, cy, tr) + tr + self.px(14)
        else:
            x += text_center(img, lab, f, B_GREY, x, cy, tr) + tr + self.px(14)
        sub = panel.edit_sub()
        if sub:
            x += text_center(img, sub.upper(), f, B_GREY, x, cy, tr) + tr
        if mode == "e_listening":
            self._vu(img, ox + cw - self.px(16), cy, panel.levels(24), False)
        self._edit_body(panel, img, ox, oy, mode, cw, ch, top, pt)
        if mode == "e_rewriting":
            self._bar(img, ox + self.px(20), oy + ch - self.px(16) - self.px(4),
                      cw - self.px(40), panel.progress())

    def _tag(self, img, x, cy, text, bg):
        """Targhetta bianca con l'icona della selezione: e' il segno di Edit in Glyph."""
        f = self.lab_font()
        tr = self.px(10.5 * 0.14)
        tw = label_width(text, f, tr)
        w = int(round(self.px(6) + self.px(13) + self.px(6) + tw + tr + self.px(8)))
        h = self.ipx(22)
        key = ("g-tag", self.k, w, h, tuple(bg))
        sp = _MASKS.get(key)
        if sp is None:
            sp = _MASKS[key] = solid((w, h), bg, rr_mask(w, h, h // 2))
        blit(img, sp, x, cy - h / 2.0)
        blit(img, sel_mark(self.k, 13, 11, (0, 0, 0), bg, 1.5, 3.0, 6.5),
             x + self.px(6), cy - self.px(11) / 2.0)
        text_center(img, text, f, (0, 0, 0), x + self.px(6 + 13 + 6), cy, tr)
        return w

    _edit_body = Stamp._edit_body

    def edit_pill(self, panel):
        f = self.lab_font()
        tr = self.px(10.5 * 0.14)
        t1, t2 = panel.edit_done_text(), " · " + panel.edit_words_text().upper()
        kw = self._keyw("CTRL+Z")
        gap = self.px(12)
        w = int(round(self.px(16) + label_width(t1, f, tr) + tr + label_width(t2, f, tr) + tr
                      + gap + kw + self.px(6) + label_width("UNDO", f, tr) + tr + self.px(10)))
        h = self.ipx(40)
        r = self.ipx(14)
        card = Image.new("RGBA", (w, h), (0, 0, 0, 255))
        card.alpha_composite(self._texture(w, h))
        card.putalpha(rr_mask(w, h, r))
        M = panel.margin
        img = Image.new("RGBA", (w + 2 * M, h + 2 * M), (0, 0, 0, 0))
        self._shadows(img, M, w, h, r)
        g = max(1, self.ipx(1))
        img.alpha_composite(solid((w + 2 * g, h + 2 * g), B_LINE, rr_mask(w + 2 * g, h + 2 * g, r + g)),
                            (M - g, M - g))
        img.alpha_composite(card, (M, M))
        cy = M + h / 2.0
        x = M + self.px(16)
        x += text_center(img, t1, f, (255, 255, 255), x, cy, tr) + tr
        x += text_center(img, t2, f, B_GREY, x, cy, tr) + tr + gap
        self._key(img, "CTRL+Z", int(x), cy)
        text_center(img, "UNDO", f, B_GREY, x + kw + self.px(6), cy, tr)
        return img

    def pill(self, panel):
        f = self.lab_font()
        tr = self.px(10.5 * 0.14)
        n = panel.words_total()
        t2 = f"{n} W · {panel.timer_text()}"
        w1 = label_width("INSERTED", f, tr) + tr
        w2 = label_width(t2, f, tr) + tr
        w = int(round(self.px(16) + self.px(8 + 8) + w1 + self.px(14 + 14) + w2 + self.px(18)))
        h = self.ipx(40 + 4 + 12)
        r = self.ipx(14)
        card = Image.new("RGBA", (w, h), (0, 0, 0, 255))
        card.alpha_composite(self._texture(w, h))
        card.putalpha(rr_mask(w, h, r))
        M = panel.margin
        img = Image.new("RGBA", (w + 2 * M, h + 2 * M), (0, 0, 0, 0))
        self._shadows(img, M, w, h, r)
        g = max(1, self.ipx(1))
        img.alpha_composite(solid((w + 2 * g, h + 2 * g), B_LINE, rr_mask(w + 2 * g, h + 2 * g, r + g)), (M - g, M - g))
        img.alpha_composite(card, (M, M))
        cy = M + self.px(20)
        x = M + self.px(16)
        put_center(img, glow_disc(self.px(4), self.px(8), (255, 255, 255), 0.8), x + self.px(4), cy)
        put_center(img, disc(self.px(4), (255, 255, 255)), x + self.px(4), cy)
        x += self.px(16)
        x += text_center(img, "INSERTED", f, (255, 255, 255), x, cy, tr) + tr + self.px(28)
        text_center(img, t2, f, B_GREY, x, cy, tr)
        self._bar(img, M + self.px(16), M + self.px(40), w - self.px(32), 1.0)
        return img


# ================================================================= D · SIGNAL
D_BG = hexc("#0C0D0F")
D_LINE = hexc("#2A2C31")
D_TEXT = hexc("#F2F3F5")
D_DIM = hexc("#6E737D")
D_LIME = hexc("#C8FF3C")
D_CUT = hexc("#FF5A4E")
D_ORANGE = hexc("#FFB547")


class Signal(Style):
    name = "signal"
    shadow_reach = 12 + 3 * 18
    shadow_dy = 16
    margin = 48
    radius = 12
    SC = 34
    text_inset = 20
    pad_r = 20
    c_commit = D_TEXT
    c_tent = hexc("#737883")
    c_gone = hexc("#5B606A")
    c_cancel = hexc("#5B606A")
    c_after = hexc("#9AA0AA")

    def body_font(self):
        return font("geist", self.px(16.5), wght=400)

    def lab_font(self):
        return font("gmono", self.px(10), wght=500)

    def r_font(self):
        return font("gmono", self.px(11), wght=500)

    def ft_font(self):
        return font("gmono", self.px(10.5), wght=500)

    def sc_h(self, m):
        return 40 + (self.SC - 40) * m

    def zones(self, mode, above, m=1.0):
        foot = 20 + 12 if self.has_foot(mode) else 0
        sc = self.sc_h(m)
        if above:                  # card sopra al caret: l'oscilloscopio sta sotto, verso il caret
            return 0, 12, 16, foot + sc
        return sc, 12, 16, foot

    def max_height(self):
        return 12 + 3 * self.line_h + 16 + 32 + self.SC

    def _label(self, mode, panel):
        if panel.preview_name:
            return panel.preview_name.upper()
        return {"listening": "LISTENING", "live": "LIVE", "paused": "PAUSED", "formatting": "FORMATTING",
                "cancelled": "CANCELLED", "offline": "OFFLINE · RECORDING",
                "recovering": "RECOVERING", "recovered": "RECOVERED", "no_audio": "NOTHING YET",
                "unrecovered": "OFFLINE"}.get(mode, "LIVE")

    def _right(self, mode, panel):
        if mode in NO_TIMER:
            return ""
        t = panel.timer_text()
        cnt = panel.count_text()
        return f"{cnt.upper()}  {t}" if cnt and mode == "live" else t

    def compact_size(self, panel, mode):
        scope = 0 if mode in RECOVER_PHASES else 150      # nel recupero non c'e' voce da disegnare
        ref = mode if mode in RECOVER_PHASES else "listening"
        lw = label_width(self._label(ref, panel), self.lab_font(), self.px(0.8)) / self.k
        rw = label_width(self._right(ref, panel) or "0:00", self.r_font(), self.px(0.22)) / self.k
        return 20 + lw + 12 + scope + 12 + rw + 16, 40

    def plate(self, panel, mode, cw, ch, above, m):
        r = self.ipx(self.radius + (20 - self.radius) * (1 - m))
        r = min(r, ch // 2)
        card = Image.new("RGBA", (cw, ch), D_BG + (255,))
        d = ImageDraw.Draw(card)
        sc = self.ipx(self.sc_h(m))
        one = max(1, self.ipx(1))
        body = ch > sc + self.px(6)
        if body:
            if above:
                d.rectangle([0, ch - sc, cw, ch - sc + one - 1], fill=hexc("#1E2024") + (255,))
            else:
                d.rectangle([0, sc - one, cw, sc - 1], fill=hexc("#1E2024") + (255,))
            if self.has_foot(mode):
                fy = (ch - sc - self.px(32)) if above else (ch - self.px(32))
                self._footer(card, mode, fy, self.px(20))
        card.putalpha(rr_mask(cw, ch, r))
        # luce interna in alto: inset 0 1px 0 rgba(255,255,255,.06)
        mk = np.asarray(rr_mask(cw, ch, r)).astype(np.int16)
        sh = np.zeros_like(mk)
        sh[one:, :] = mk[:-one, :]
        edge = np.clip(mk - sh, 0, 255).astype(np.uint8)
        card.alpha_composite(solid((cw, ch), (255, 255, 255), Image.fromarray(edge, "L"), 0.06))
        M = panel.margin

        def draw(img, cw, ch):
            # 0 16px 36px -12px rgba(0,0,0,.5) + 0 2px 6px rgba(0,0,0,.25)
            sp, dy1, dy2 = self.px(12), self.px(16), self.px(2)
            img.alpha_composite(shadow_image(img.width, img.height, [
                (M + sp, M + sp + dy1, M + cw - sp, M + ch - sp + dy1, self.px(36) / 2.0, 0.5),
                (M, M + dy2, M + cw, M + ch + dy2, self.px(6) / 2.0, 0.25)]))
            img.alpha_composite(solid((cw + 2 * one, ch + 2 * one), D_LINE,
                                      rr_mask(cw + 2 * one, ch + 2 * one, r + one)), (M - one, M - one))
        img = self.backdrop(M, cw, ch, r, draw)
        img.alpha_composite(card, (M, M))
        return img

    def _key(self, dst, text, x, cy):
        f = self.ft_font()
        tr = self.px(10.5 * 0.06)
        tw = label_width(text, f, tr)
        w = int(round(tw + self.px(7) * 2 + tr))
        h = self.ipx(20)
        y = int(round(cy - h / 2.0))
        bw = max(1, self.ipx(1))
        r = self.ipx(5)
        blit(dst, solid((w, h), hexc("#2E3137"), rr_mask(w, h, r)), x, y)
        blit(dst, solid((w - 2 * bw, h - 2 * bw), hexc("#1B1D21"), rr_mask(w - 2 * bw, h - 2 * bw, r - bw)), x + bw, y + bw)
        text_center(dst, text, f, D_TEXT, x + self.px(7), cy, tr)
        return w

    def _keyw(self, text):
        tr = self.px(10.5 * 0.06)
        return int(round(label_width(text, self.ft_font(), tr) + self.px(7) * 2 + tr))

    def _footer(self, card, mode, fy, fh):
        if mode[:2] == "e_":
            return self._efooter(card, mode, fy, fh)
        f = self.ft_font()
        tr = self.px(10.5 * 0.06)
        cy = fy + fh / 2.0
        x0 = self.px(20)
        xr = card.width - self.px(16)
        gap = self.px(6)
        if mode == "paused":
            x = x0 + self._key(card, "WIN+CTRL", int(x0), cy) + gap
            text_center(card, "INSERT", f, D_DIM, x, cy, tr)
            tw = label_width("CANCEL", f, tr)
            kw = self._keyw("ESC")
            x = xr - tw - gap - kw
            self._key(card, "ESC", int(x), cy)
            text_center(card, "CANCEL", f, D_DIM, x + kw + gap, cy, tr)
        elif mode == "cancelled":
            text_center(card, "NOTHING INSERTED", f, D_DIM, x0, cy, tr)
            tw = label_width("BRINGS IT BACK", f, tr)
            kw = self._keyw("WIN+CTRL+R")
            x = xr - tw - gap - kw
            self._key(card, "WIN+CTRL+R", int(x), cy)
            text_center(card, "BRINGS IT BACK", f, D_DIM, x + kw + gap, cy, tr)
        elif mode == "offline":
            text_center(card, "PREVIEW OFF", f, D_DIM, x0, cy, tr)
            text_center(card, "TEXT LANDS WHEN YOU STOP", f, D_DIM, xr, cy, tr, right=True)
        elif mode in R_FOOTERS:
            left, key, right = R_FOOTERS[mode]
            text_center(card, left.upper(), f, D_DIM, x0, cy, tr)
            tw = label_width(right.upper(), f, tr)
            kw = self._keyw(key.upper())
            x = xr - tw - gap - kw
            self._key(card, key.upper(), int(x), cy)
            text_center(card, right.upper(), f, D_DIM, x + kw + gap, cy, tr)

    def front(self, panel, img, ox, oy, mode, cw, ch, above, m, now):
        sc = self.px(self.sc_h(m))
        top = (oy + ch - sc) if (above and ch > sc + self.px(6)) else oy
        cy = top + sc / 2.0
        if ch <= sc + self.px(6):
            cy = oy + ch / 2.0
        x = ox + self.px(20)
        lab = self._label(mode, panel)
        x += text_center(img, lab, self.lab_font(), D_TEXT, x, cy, self.px(0.8)) + self.px(12)
        right = self._right(mode, panel)
        xr = ox + cw - self.px(16)
        if right:
            rw = label_width(right, self.r_font(), self.px(0.22))
            text_center(img, right, self.r_font(), hexc("#C9CDD4"), xr, cy, self.px(0.22), right=True)
            xr -= rw + self.px(12)
        w = xr - x
        if w > 8 and mode not in RECOVER_PHASES:
            self._scope(img, x, cy, w, mode, panel, now)

    def _scope(self, img, x0, cy, w, mode, panel, now, col=None):
        """Oscilloscopio: portante fine sotto un inviluppo morbido dalla storia del livello,
        che scorre verso destra (il presente). Sfuma da sinistra, testa luminosa sul presente."""
        k = self.k
        W = int(round(w))
        H = self.ipx(34)
        col = col or {"cancelled": D_CUT, "offline": D_ORANGE, "paused": D_DIM}.get(mode, D_LIME)
        base = H / 2.0
        out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(out)
        d.line([(0, int(base)), (W, int(base))], fill=hexc("#23262B") + (255,), width=max(1, int(k)))
        if mode == "formatting":
            p = panel.progress()
            xp = int(round(W * p))
            lw = self.px(1.5)
            if xp > 0:
                blit(out, solid((xp, max(1, int(round(lw)))), col, Image.new("L", (xp, max(1, int(round(lw)))), 255)),
                     0, base - lw / 2.0)
            bh = self.ipx(16)
            bw = max(2, self.ipx(2))
            blit(out, solid((bw, bh), col, rr_mask(bw, bh, bw // 2)), xp, base - bh / 2.0)
            blit(img, out, x0, cy - H / 2.0)
            return
        flat = mode in ("paused", "cancelled")
        S = 2
        xs = np.arange(0, W * S + 1, dtype=np.float32) / S          # px fisici
        if flat:
            ys = np.full_like(xs, base)
        else:
            lv, frac = panel.level_track(32)
            n = len(lv)
            f = xs / max(1.0, W) * (n - 1) + frac
            i = np.clip(np.floor(f).astype(int), 0, n - 1)
            t = f - np.floor(f)
            a = lv[i]
            b = lv[np.clip(i + 1, 0, n - 1)]
            kk = (1 - np.cos(t * np.pi)) / 2
            env = a + (b - a) * kk
            xl = xs / k + frac * (w / k) / max(1, n - 1)            # la portante scorre con la storia
            ys = base - np.sin(xl * 2 * np.pi / 5.5) * env * 11 * k * (0.75 + 0.25 * np.sin(xl * 0.21))
        pts = list(zip((xs * S).tolist(), (ys * S).tolist()))
        line = Image.new("L", (W * S, H * S), 0)
        ImageDraw.Draw(line).line(pts, fill=255, width=max(1, int(round(1.2 * k * S))), joint="curve")
        line = line.resize((W, H), Image.BILINEAR)
        glow = Image.new("L", (W, H), 0)
        ImageDraw.Draw(glow).line([(p[0] / S, p[1] / S) for p in pts[::S]], fill=255, width=max(1, int(round(2.6 * k))))
        glow = glow.filter(ImageFilter.GaussianBlur(1.8 * k))
        # rampa: 0 a sinistra, .5 a meta', 1 a destra
        ramp = self._ramp(W)
        la = np.asarray(line).astype(np.float32) / 255.0 * ramp
        ga = np.asarray(glow).astype(np.float32) / 255.0 * ramp * 0.5
        a = 1 - (1 - la) * (1 - ga)
        sp = Image.fromarray(np.dstack([np.full((H, W), c, np.uint8) for c in col]
                                       + [np.clip(a * 255, 0, 255).astype(np.uint8)]), "RGBA")
        out.alpha_composite(sp)
        put_center(out, disc(self.px(2.6), col), W - self.px(3), base)   # la testa: il presente
        blit(img, out, x0, cy - H / 2.0)

    def _ramp(self, W):
        key = ("d-ramp", W)
        r = _MASKS.get(key)
        if r is None:
            x = np.linspace(0, 1, W, dtype=np.float32)
            r = x[None, :]                      # 0 a sinistra, .5 a meta', 1 a destra
            _MASKS[key] = r
        return r

    def strike(self, layer, x, line_y, w, p, alpha):
        """1,5 px rosso al 54% con alone (0 0 6px rgba(255,90,78,.6))."""
        ext = self.px(2)
        L = int((w + 2 * ext) * p)
        if L < 1:
            return
        key = ("d-strike", L, round(alpha, 2), self.k)
        sp = _MASKS.get(key)
        if sp is None:
            pad = self.ipx(8)
            h = self.px(1.5)
            H = int(math.ceil(h)) + 2 * pad
            m = Image.new("L", (L + 2 * pad, H), 0)
            ImageDraw.Draw(m).rectangle([pad, pad, pad + L - 1, pad + h], fill=255)
            glow = m.filter(ImageFilter.GaussianBlur(self.px(6) / 2.0)).point(lambda v: int(v * 0.6 * alpha))
            sp = solid(m.size, D_CUT, glow)
            core = Image.new("L", m.size, 0)
            ImageDraw.Draw(core).rectangle([pad, pad, pad + L - 1, pad + h - 0.01], fill=int(255 * alpha))
            sp.alpha_composite(solid(m.size, D_CUT, core))
            if len(_MASKS) > 600:
                _MASKS.clear()
            _MASKS[key] = sp
        pad = self.ipx(8)
        blit(layer, sp, x - ext - pad, line_y + self.px(self.line_h) * 0.54 - self.px(0.75) - pad)

    # -------- Edit Mode
    # Il segno di Edit e' l'icona della selezione + "EDIT" nella riga dello strumento (in
    # dettatura li' c'e' "LIVE" senza icona).
    E_ICE = hexc("#6FD6FF")
    E_LAB = {"e_listening": ("EDIT", True), "e_rewriting": ("REWRITING", True),
             "e_offline": ("EDIT", True), "e_cancelled": ("CANCELLED", False),
             "e_nothing": ("NOTHING SELECTED", False)}
    E_COL = {"e_offline": D_ORANGE, "e_cancelled": D_CUT, "e_nothing": D_DIM}
    edit_line_h = 22
    edit_inset = 20
    edit_pad_r = 20
    E_CHIP_PAD = 14
    E_CHIP_H = 30
    E_CHIP_BOT = 14
    E_CHIP_IN = 8
    E_KBD_W = 17
    ec_commit = D_TEXT
    ec_tent = hexc("#737883")
    ec_gone = hexc("#5B606A")
    ec_ph = hexc("#737883")
    ec_you = D_DIM

    def edit_body_font(self):
        return font("geist", self.px(15), wght=400)

    def edit_you_font(self):
        return font("gmono", self.px(10), wght=500)

    def edit_you_tr(self):
        return self.px(10 * 0.08)

    def edit_chip_font(self):
        return font("geist", self.px(13), wght=500)

    def edit_kbd_font(self):
        return font("gmono", self.px(10.5), wght=500)

    def edit_foot_h(self):
        return 20 + 12

    def edit_zones(self, mode, above, m=1.0, chip=True):
        sc = self.SC
        has_chip = mode in E_CHIPS_ONE and chip
        body = self.edit_chips_h() if has_chip else 0
        if mode in E_FOOT:
            body += self.edit_foot_h() + (0 if has_chip else 14)
        if mode in E_SAID or mode == "e_done":
            if above:
                return 0, self.edit_chips_h(), 12, sc
            return sc, self.edit_chips_h(), 12, 0
        if above:
            return 0, 0, 0, body + sc
        return sc, 0, 0, body

    def edit_max_height(self):
        t, pt, pb, b = self.edit_zones("e_listening", True)
        a = t + pt + 2 * self.edit_line_h + pb + b
        t, _pt, _pb, b = self.edit_zones("e_offline", True)
        return max(a, t + b)

    def _chip(self, img, x, y, w, h, chip, state, full, tab=None):
        """Chip a fondo pieno con filo interno; acceso = filo e numero color ghiaccio."""
        W, H = max(2, int(round(w))), max(2, int(round(h)))
        key = ("d-chip", self.k, W, H, chip.id, state, full)
        sp = _MASKS.get(key)
        if sp is None:
            r, one = self.ipx(7), max(1, self.ipx(1))
            on = state == "on"
            ring = self.E_ICE if on else D_LINE
            sp = solid((W, H), ring, rr_mask(W, H, r))
            fill = tuple(int(round(a + (b_ - a) * 0.12)) for a, b_ in zip(D_BG, self.E_ICE)) if on \
                else hexc("#16181B")
            sp.alpha_composite(solid((W - 2 * one, H - 2 * one), fill,
                                     rr_mask(W - 2 * one, H - 2 * one, r - one)), (one, one))
            kw, kh = self.edit_kbd_w(), self.px(17)
            tw = self.chip_text_w(chip.label) if full else 0.0
            inner = kw + (self.px(self.E_CHIP_IN) + tw if full else 0.0)
            cx, cy = (W - inner) / 2.0, H / 2.0
            kf = self.edit_kbd_font()
            blit(sp, solid((int(round(kw)), int(round(kh))), self.E_ICE if on else hexc("#23262B"),
                           rr_mask(int(round(kw)), int(round(kh)), self.ipx(4))), cx, cy - kh / 2.0)
            text_center(sp, chip.key, kf, D_BG if on else hexc("#9AA0AA"),
                        cx + (kw - label_width(chip.key, kf)) / 2.0, cy)
            if full:
                text_center(sp, chip.label, self.edit_chip_font(),
                            (255, 255, 255) if on else hexc("#C9CDD4"),
                            cx + kw + self.px(self.E_CHIP_IN), cy)
            if state == "off":
                sp.putalpha(sp.getchannel("A").point(lambda v: int(v * 0.35)))
            if len(_MASKS) > 600:
                _MASKS.clear()
            _MASKS[key] = sp
        blit(img, sp, x, y)

    def _efooter(self, card, mode, fy, fh):
        f = self.ft_font()
        tr = self.px(10.5 * 0.06)
        cy = fy + fh / 2.0
        x0, xr = self.px(20), card.width - self.px(16)
        gap = self.px(6)
        left, key, right = E_FOOTERS[mode]
        text_center(card, left.upper(), f, D_DIM, x0, cy, tr)
        if key:
            tw = label_width(right.upper(), f, tr)
            kw = self._keyw(key.upper())
            x = xr - tw - gap - kw
            self._key(card, key.upper(), int(x), cy)
            text_center(card, right.upper(), f, D_DIM, x + kw + gap, cy, tr)

    def edit_front(self, panel, img, ox, oy, mode, cw, ch, above, m, now):
        top, pt, _pb, _bot = (v * self.k for v in self.edit_zones(mode, above, m))
        sc = self.px(self.SC)
        scy = (oy + ch - sc) if above else oy
        cy = scy + sc / 2.0
        x = ox + self.px(20)
        lab, icon = self.E_LAB.get(mode, ("EDIT", True))
        col = self.E_COL.get(mode, self.E_ICE)
        if icon:
            blit(img, sel_mark(self.k, 13, 11, col, D_BG, 1.5, 3.0, 6.5), x, cy - self.px(11) / 2.0)
            x += self.px(13 + 7)
        tr = self.px(0.8)
        x += text_center(img, lab, self.lab_font(), D_TEXT, x, cy, tr) + tr
        sub = panel.edit_sub()
        if sub:
            x += self.px(7) + text_center(img, sub.upper(), self.lab_font(), D_DIM,
                                          x + self.px(7), cy, tr) + tr
        x += self.px(12)
        w = ox + cw - self.px(16) - x
        if w > 8:
            self._scope(img, x, cy, w, "formatting" if mode == "e_rewriting" else
                        {"e_cancelled": "cancelled", "e_offline": "offline",
                         "e_nothing": "paused"}.get(mode, "live"), panel, now, col)
        self._edit_body(panel, img, ox, oy, mode, cw, ch, top, pt)

    _edit_body = Stamp._edit_body

    def edit_pill(self, panel):
        f = self.ft_font()
        tr = self.px(10.5 * 0.06)
        t1, t2 = panel.edit_done_text(), panel.edit_words_text().upper()
        kw = self._keyw("CTRL+Z")
        gap = self.px(10)
        w = int(round(self.px(14) + self.px(6) + self.px(10) + label_width(t1, f, tr) + tr
                      + gap + label_width(t2, f, tr) + tr + gap + kw + self.px(6)
                      + label_width("UNDO", f, tr) + tr + self.px(8)))
        h = self.ipx(36)
        r = h // 2
        M = panel.margin
        img = Image.new("RGBA", (w + 2 * M, h + 2 * M), (0, 0, 0, 0))
        sp = self.ipx(10)
        img.alpha_composite(shadow_image(img.width, img.height, [
            (M + sp, M + sp + self.px(12), M + w - sp, M + h - sp + self.px(12), self.px(26) / 2.0, 0.5)]))
        one = max(1, self.ipx(1))
        img.alpha_composite(solid((w + 2 * one, h + 2 * one), D_LINE,
                                  rr_mask(w + 2 * one, h + 2 * one, r + one)), (M - one, M - one))
        img.alpha_composite(solid((w, h), D_BG, rr_mask(w, h, r)), (M, M))
        cy = M + h / 2.0
        x = M + self.px(14)
        put_center(img, glow_disc(self.px(3), self.px(8), self.E_ICE, 1.0), x + self.px(3), cy)
        put_center(img, disc(self.px(3), self.E_ICE), x + self.px(3), cy)
        x += self.px(16)
        x += text_center(img, t1, f, D_TEXT, x, cy, tr) + tr + gap
        x += text_center(img, t2, f, D_DIM, x, cy, tr) + tr + gap
        self._key(img, "CTRL+Z", int(x), cy)
        text_center(img, "UNDO", f, D_DIM, x + kw + self.px(6), cy, tr)
        return img

    def pill(self, panel):
        f = self.ft_font()
        tr = self.px(10.5 * 0.06)
        n = panel.words_total()
        t2 = f"{n} W · {panel.timer_text()}"
        w1 = label_width("INSERTED", f, tr) + tr
        w2 = label_width(t2, f, tr) + tr
        w = int(round(self.px(14) + self.px(6) + self.px(10) + w1 + self.px(10) + w2 + self.px(14)))
        h = self.ipx(36)
        r = h // 2
        M = panel.margin
        img = Image.new("RGBA", (w + 2 * M, h + 2 * M), (0, 0, 0, 0))
        sp = self.ipx(10)
        img.alpha_composite(shadow_image(img.width, img.height, [
            (M + sp, M + sp + self.px(12), M + w - sp, M + h - sp + self.px(12), self.px(26) / 2.0, 0.5)]))
        one = max(1, self.ipx(1))
        img.alpha_composite(solid((w + 2 * one, h + 2 * one), D_LINE, rr_mask(w + 2 * one, h + 2 * one, r + one)), (M - one, M - one))
        img.alpha_composite(solid((w, h), D_BG, rr_mask(w, h, r)), (M, M))
        cy = M + h / 2.0
        x = M + self.px(14)
        put_center(img, glow_disc(self.px(3), self.px(8), D_LIME, 1.0), x + self.px(3), cy)
        put_center(img, disc(self.px(3), D_LIME), x + self.px(3), cy)
        x += self.px(16)
        x += text_center(img, "INSERTED", f, D_TEXT, x, cy, tr) + tr + self.px(10)
        text_center(img, t2, f, D_DIM, x, cy, tr)
        return img


_CLASSES = {"stamp": Stamp, "glyph": Glyph, "signal": Signal}


def make(name, k=1.0):
    return _CLASSES.get(name, Signal)(k)
