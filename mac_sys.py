"""
mac_sys.py — il resto del sistema su macOS: appunti, incolla (Cmd+V), copia (Cmd+C), app da
riattivare, bip, istanza unica, permessi, nessuna icona nel Dock.

Stesse funzioni che wavetype.py usa su Windows (get/set_clipboard_text, insert_text,
copy_selection), stesso contratto. Tutto in try/except: la dettatura non deve mai cadere per
colpa di questo strato.

Incolla (spec mac): appunti con il testo marcato org.nspasteboard.TransientType (i gestori di
appunti non lo salvano, come fa FreeFlow), 0,1 s di attesa, Cmd+V sintetico con CGEventPost.
Gli appunti di prima tornano dopo ~1 s SOLO se nel frattempo nessuno li ha toccati
(changeCount uguale) e contengono ancora il testo dettato.
Prima dell'incolla l'app bersaglio deve risultare davanti (activate): se e' chiusa o non torna
davanti, niente Cmd+V e il testo resta negli appunti, senza ripristino.
"""
import ctypes
import ctypes.util
import os
import queue
import subprocess
import threading
import time

import numpy as np

TRANSIENT = "org.nspasteboard.TransientType"
STRING = "public.utf8-plain-text"                 # = NSPasteboardTypeString
RESTORE_AFTER = 1.0
PASTE_WAIT = 0.10
KC_V_DEFAULT, KC_C_DEFAULT = 9, 8                 # posizioni ANSI: QWERTY, AZERTY, QWERTZ
SNAP_TEXT = (STRING, "public.rtf", "public.html", "public.url", "public.file-url")
SNAP_IMAGE = ("public.png", "public.tiff")

URL_MIC = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
URL_AX = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
URL_KEYBOARD = "x-apple.systempreferences:com.apple.Keyboard-Settings.extension"

_LOG = [print]


def set_log(fn):
    _LOG[0] = fn


def log(msg):
    try:
        _LOG[0](msg)
    except Exception:
        pass


# ------------------------------------------------------------------ appunti
def _pb():
    from AppKit import NSPasteboard
    return NSPasteboard.generalPasteboard()


def get_clipboard_text():
    try:
        s = _pb().stringForType_(STRING)
        return None if s is None else str(s)
    except Exception:
        return None


def set_clipboard_text(text, transient=False):
    pb = _pb()
    pb.clearContents()
    pb.setString_forType_(text, STRING)
    if transient:
        pb.setString_forType_("", TRANSIENT)
    return int(pb.changeCount())


def snapshot_clipboard():
    """Il contenuto degli appunti (ogni elemento) nei tipi di SNAP_TEXT e SNAP_IMAGE: un'immagine
    copiata prima della dettatura torna com'era, non solo il testo. Non ogni tipo: quelli dati
    "a richiesta" (celle di Excel o Numbers, file promessi) costringono l'app di origine a
    generarli tutti, qui, sul thread di Tk. Le immagini solo se l'elemento non ha testo (credo
    che i fogli di calcolo offrano anche un'immagine dell'intervallo, la piu' lenta da fare).
    None se non si riesce a leggerlo."""
    try:
        items = []
        for it in _pb().pasteboardItems() or []:
            d = {}
            types = [str(t) for t in it.types() or []]
            keep = SNAP_TEXT + (() if STRING in types else SNAP_IMAGE)
            for t in types:
                if t not in keep:
                    continue
                data = it.dataForType_(t)
                if data is not None:
                    d[str(t)] = data
            if d:
                items.append(d)
        return items
    except Exception:
        return None


def restore_clipboard(items):
    from AppKit import NSPasteboardItem
    pb = _pb()
    pb.clearContents()
    objs = []
    for d in items:
        it = NSPasteboardItem.alloc().init()
        for t, data in d.items():
            it.setData_forType_(data, t)
        objs.append(it)
    if objs:
        pb.writeObjects_(objs)


# ------------------------------------------------------------------ tasti sintetici
_carbon = {"lib": None, "tried": False}
_keycodes = {}                    # lettera -> keycode, scritto solo dal thread principale
KEYCODES_EVERY = 5.0              # il tick lo ricalcola ogni tanti secondi (cambio di layout)


def refresh_keycodes():
    """Ricalcola i keycode di V e C. Solo dal thread principale (tick di Tk)."""
    for letter, default in (("v", KC_V_DEFAULT), ("c", KC_C_DEFAULT)):
        _letter_keycode(letter, default)


def _letter_keycode(letter, default):
    """Codice tasto che produce `letter` con Command premuto nel layout attivo (Dvorak sposta la
    V; "Dvorak - QWERTY Cmd" invece la tiene dov'e' con Command, per questo si traduce CON
    Command). Carbon via ctypes, come fa pynput. Qualunque intoppo: la posizione ANSI.

    Le funzioni TIS vanno chiamate dal thread principale (credo che da macOS 14 fuori da li'
    il processo muoia con un trap, che nessun try ferma): dagli altri thread (copia della
    selezione dal worker) si legge solo l'ultimo valore calcolato da quello principale."""
    if threading.current_thread() is not threading.main_thread():
        return _keycodes.get(letter, default)
    kc = _letter_keycode_tis(letter, default)
    _keycodes[letter] = kc
    return kc


def _letter_keycode_tis(letter, default):
    try:
        if not _carbon["tried"]:
            _carbon["tried"] = True
            _carbon["lib"] = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/Carbon.framework/Carbon")
            cf = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            _carbon["cf"] = cf
        carbon, cf = _carbon["lib"], _carbon.get("cf")
        if carbon is None or cf is None:
            return default
        carbon.TISCopyCurrentKeyboardLayoutInputSource.restype = ctypes.c_void_p
        carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
        carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFDataGetBytePtr.restype = ctypes.c_void_p
        cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        carbon.LMGetKbdType.restype = ctypes.c_uint8
        carbon.UCKeyTranslate.restype = ctypes.c_int32
        carbon.UCKeyTranslate.argtypes = [
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_uint16)]
        key = ctypes.c_void_p.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData")
        src = carbon.TISCopyCurrentKeyboardLayoutInputSource()
        if not src:
            return default
        try:
            data = carbon.TISGetInputSourceProperty(src, key)
            if not data:
                return default
            layout = cf.CFDataGetBytePtr(data)
            kbd = carbon.LMGetKbdType()
            cmd = (0x0100 >> 8) & 0xFF                     # cmdKey, nel formato di UCKeyTranslate
            for kc in range(128):
                dead = ctypes.c_uint32(0)
                n = ctypes.c_ulong(0)
                buf = (ctypes.c_uint16 * 4)()
                st = carbon.UCKeyTranslate(layout, kc, 3, cmd, kbd, 1, ctypes.byref(dead), 4,
                                           ctypes.byref(n), buf)   # 3 = kUCKeyActionDisplay
                if st == 0 and n.value == 1 and chr(buf[0]).lower() == letter:
                    return kc
        finally:
            cf.CFRelease(src)
    except Exception:
        pass
    return default


def send_cmd(letter, default):
    """Cmd+<lettera> sintetico. I flag si impostano a mano: Fn o Ctrl+Option ancora tenuti
    (modo "tieni premuto", copia della selezione in Edit) non devono sporcare la scorciatoia."""
    import Quartz
    kc = _letter_keycode(letter, default)
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(src, kc, down)
        Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.005)


FRONT_WAIT = 0.30        # dopo la richiesta di attivazione: fino a tanto si aspetta che l'app sia davanti
FRONT_POLL = 0.02
# Se e' servito riportare davanti l'app, prima del Cmd+V si aspetta ancora questo (oltre a
# PASTE_WAIT). Il pid risulta davanti appena macOS cambia app, ma credo che la finestra prenda
# il focus solo a fine animazione (cambio di Space, app a tutto schermo: credo ~0,3 s). Con
# PASTE_WAIT fa 0,35 s dopo la conferma: un tasto perso costa piu' di un'attesa, e capita solo
# quando l'utente ha cambiato app durante la formattazione.
ACTIVATED_WAIT = 0.25
UNKNOWN = "unknown"      # activate(): non si sa chi e' davanti (pid 0, AppKit e AX muti)
ACTIVATED = "activated"  # activate(): non era davanti, ce l'abbiamo riportata (e confermato)
PASTE = {"why": ""}      # perche' l'ultimo insert_text non ha incollato (log, testhooks)


def _ax_front_pid():
    """pid dell'app con focus secondo AX (kAXFocusedApplicationAttribute dell'elemento di
    sistema, timeout 0,1 s): domanda viva al server di accessibilita'. None se AX non risponde."""
    try:
        import ApplicationServices as AS
        sysw = AS.AXUIElementCreateSystemWide()
        try:
            AS.AXUIElementSetMessagingTimeout(sysw, 0.1)
        except Exception:
            pass
        err, app = AS.AXUIElementCopyAttributeValue(sysw, AS.kAXFocusedApplicationAttribute, None)
        if err == AS.kAXErrorSuccess and app is not None:
            err, p = AS.AXUIElementGetPid(app, None)
            if err == AS.kAXErrorSuccess and p:
                return int(p)
    except Exception:
        pass
    return None


def _ns_front_pid():
    """pid di NSWorkspace.frontmostApplication, None se non risponde."""
    try:
        from AppKit import NSWorkspace
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None:
            return int(front.processIdentifier())
    except Exception:
        pass
    return None


def _front_pid():
    """Chi e' davanti: decide AX quando risponde, NSWorkspace solo se AX tace; 0 = nessuno dei
    due. NSWorkspace credo si aggiorni solo quando gira il run loop principale, e insert_text
    gira proprio li' (tick di Tk): puo' essere fermo a un istante prima e dire ancora "Notes"
    quando davanti c'e' gia' Slack. Con "basta una delle due" quel caso incollava in Slack."""
    p = _ax_front_pid()
    if p is None:
        p = _ns_front_pid()
    return p or 0


def _running_app(pid):
    from AppKit import NSRunningApplication
    return NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))


def activate(pid):
    """Riporta davanti l'app bersaglio se nel frattempo non lo e' piu', e RICONTROLLA che ci sia
    arrivata (fino a FRONT_WAIT). True = era gia' davanti. ACTIVATED = non lo era, ce l'abbiamo
    riportata e risulta davanti. False = chiesto ma non arrivata: da macOS 14 l'attivazione e'
    "cooperativa" e IgnoringOtherApps e' deprecato, credo che un'app Accessory non riesca sempre
    a portarne davanti un'altra. None = l'app non c'e' piu'. UNKNOWN = non si sa (pid 0, AppKit
    e AX che non rispondono): si incolla come prima, nell'app davanti.
    Il pid puo' essere il nostro (finestra del primo avvio davanti al tasto): vale lo stesso."""
    if not pid:
        return UNKNOWN
    pid = int(pid)
    try:
        if _front_pid() == pid:
            return True
        app = _running_app(pid)
        if app is None:
            return None
        app.activateWithOptions_(1 << 1)        # NSApplicationActivateIgnoringOtherApps
    except Exception as e:
        log(f"   [mac] riattivazione app {pid}: {e}")
        return UNKNOWN
    end = time.perf_counter() + FRONT_WAIT
    seen = False
    while True:
        time.sleep(FRONT_POLL)
        front = _front_pid()
        if front == pid:
            return ACTIVATED
        seen = seen or bool(front)
        if time.perf_counter() >= end:
            return False if seen else UNKNOWN


def insert_text(pid, text):
    """Incolla `text` nell'app `pid` (spec mac). Non solleva. False = non incollato, il perche'
    in PASTE["why"]."""
    PASTE["why"] = ""
    act = activate(pid)
    if act is None or act is False:
        # None: app bersaglio chiusa durante la formattazione. False: non e' tornata davanti
        # (detti in Notes, durante la formattazione passi a Slack e macOS non concede il cambio).
        # In tutti e due i casi Cmd+V finirebbe nell'app davanti adesso: niente Cmd+V, il testo
        # resta negli appunti (senza TransientType e senza ripristino), da incollare a mano.
        try:
            set_clipboard_text(text)
        except Exception:
            pass
        PASTE["why"] = (f"app {pid} chiusa" if act is None else
                        f"app {pid} non e' tornata davanti entro {FRONT_WAIT:.1f} s")
        log(f"   [mac] {PASTE['why']}: niente incolla, testo lasciato negli appunti")
        return False
    # App riportata davanti da noi: il Cmd+V puo' perdersi durante il cambio (finestra non
    # ancora col focus). Allora gli appunti di prima NON tornano e il testo resta negli appunti
    # (senza TransientType, come negli altri ripieghi): se l'incolla si e' perso, Cmd+V a mano.
    moved = act == ACTIVATED
    saved = None if moved else snapshot_clipboard()
    try:
        cc = set_clipboard_text(text, transient=not moved)
    except Exception as e:
        PASTE["why"] = f"appunti non scritti: {e}"
        log(f"   [mac] {PASTE['why']}")
        return False
    if moved:
        log(f"   [mac] app {pid} riportata davanti: attendo {ACTIVATED_WAIT:.2f} s in piu', "
            "appunti di prima non ripristinati")
        time.sleep(ACTIVATED_WAIT)
    time.sleep(PASTE_WAIT)
    try:
        send_cmd("v", KC_V_DEFAULT)
    except Exception as e:
        PASTE["why"] = f"Cmd+V non inviato: {e}"
        log(f"   [mac] {PASTE['why']}")
        return False
    if saved:
        def restore():
            try:
                pb = _pb()
                if int(pb.changeCount()) == cc and get_clipboard_text() == text:
                    restore_clipboard(saved)
            except Exception:
                pass
        threading.Timer(RESTORE_AFTER, restore).start()
    return True


def copy_selection(pid=None):
    """La selezione dell'app in primo piano. Prima AX (niente tasti, niente bip "non si puo'
    copiare" nelle app senza selezione); se l'app non risponde via AX, Cmd+C come su Windows."""
    try:
        import mac_ax
        s = mac_ax.selected_text(pid)
        if s is not None:
            return s.strip()
    except Exception:
        pass
    saved = snapshot_clipboard()
    try:
        set_clipboard_text("")
    except Exception:
        pass
    try:
        send_cmd("c", KC_C_DEFAULT)
    except Exception as e:
        log(f"   [mac] Cmd+C non inviato: {e}")
    time.sleep(0.12)
    sel = (get_clipboard_text() or "").strip()
    if saved is not None:
        try:
            restore_clipboard(saved)
        except Exception:
            pass
    return sel


# ------------------------------------------------------------------ bip
BEEP_SR = 44100
_beeps = {"q": None}


def tone(freq, ms, sr=BEEP_SR, vol=0.25):
    """Seno con 5 ms di attacco e rilascio (senza, il bip fa "clic")."""
    n = max(1, int(sr * ms / 1000.0))
    t = np.arange(n, dtype="float32") / sr
    a = (vol * np.sin(2 * np.pi * float(freq) * t)).astype("float32")
    r = min(n // 2, int(sr * 0.005))
    if r > 0:
        ramp = np.linspace(0.0, 1.0, r, dtype="float32")
        a[:r] *= ramp
        a[-r:] *= ramp[::-1]
    return a


def _beeper():
    import sounddevice as sd
    q = _beeps["q"]
    while True:
        freq, ms = q.get()
        try:
            sd.play(tone(freq, ms), BEEP_SR, blocking=True)
        except Exception:
            time.sleep(ms / 1000.0)


def beep(freq, ms=90):
    """Come winsound.Beep ma senza bloccare chi chiama: i toni vanno in coda a un thread solo,
    cosi' due bip di fila (beep_silence) suonano uno dopo l'altro e mai sovrapposti."""
    try:
        if _beeps["q"] is None:
            _beeps["q"] = queue.Queue()
            threading.Thread(target=_beeper, name="beep", daemon=True).start()
        _beeps["q"].put((freq, ms))
    except Exception:
        pass


# ------------------------------------------------------------------ istanza unica
def single_instance(lock_path):
    """Lock esclusivo su un file (fcntl): il file aperto va tenuto vivo per tutta la sessione.
    None = c'e' gia' un Wavetype che gira."""
    import fcntl
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    f = open(lock_path, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    try:
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
    except Exception:
        pass
    return f


# ------------------------------------------------------------------ app senza Dock, permessi
def set_accessory():
    """Niente icona nel Dock e niente menu (dal sorgente: nel bundle lo fa LSUIElement=1)."""
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        return True
    except Exception as e:
        log(f"[mac] policy Accessory non applicata: {e}")
        return False


def dark_titlebar(title, rgb):
    """Barra del titolo della finestra Tk `title` scura e dello stesso colore del fondo, come
    _dark_titlebar su Windows: aspetto DarkAqua (titolo chiaro) e barra trasparente sul colore
    della finestra. Torna True se la finestra c'era."""
    try:
        from AppKit import NSApplication, NSAppearance, NSColor
        try:
            from AppKit import NSAppearanceNameDarkAqua as dark_name
        except ImportError:
            dark_name = "NSAppearanceNameDarkAqua"
        dark = NSAppearance.appearanceNamed_(dark_name)
        r, g, b = (c / 255.0 for c in rgb[:3])
        for w in NSApplication.sharedApplication().windows():
            if str(w.title() or "") != title:
                continue
            w.setAppearance_(dark)
            w.setTitlebarAppearsTransparent_(True)
            w.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0))
            return True
        log(f"[first_run] barra scura: nessuna finestra '{title}'")
    except Exception as e:
        log(f"[first_run] barra scura non applicata: {e}")
    return False


def ax_trusted(prompt=False):
    """Permesso Accessibilita'. Con prompt=True macOS mostra il suo avviso (una volta)."""
    try:
        import ApplicationServices as AS
        if prompt:
            return bool(AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True}))
        return bool(AS.AXIsProcessTrusted())
    except Exception:
        return False


def mic_status():
    """"granted" | "denied" | "undetermined" | "unknown" (AVFoundation mancante)."""
    try:
        import AVFoundation as AV
        st = int(AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio))
    except Exception:
        return "unknown"
    return {3: "granted", 2: "denied", 1: "denied", 0: "undetermined"}.get(st, "unknown")


def request_mic():
    """Chiede il microfono (avviso di sistema la prima volta). Non blocca."""
    try:
        import AVFoundation as AV
        AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AV.AVMediaTypeAudio, lambda ok: None)
        return True
    except Exception as e:
        log(f"[mac] richiesta microfono: {e}")
        return False


def permissions_ok():
    return ax_trusted() and mic_status() in ("granted", "unknown")


def open_url(url):
    """Apre un pannello delle Impostazioni di Sistema (o una pagina web)."""
    try:
        subprocess.Popen(["open", url])
        return True
    except Exception as e:
        log(f"[mac] open {url}: {e}")
        return False
