"""
mac_panel.py — la finestra della card su macOS: un NSPanel che mostra l'immagine PIL RGBA.

Su Windows la card e' una finestra layered (UpdateLayeredWindow, alpha per-pixel,
WS_EX_TRANSPARENT = i click passano sotto). Qui lo stesso con un NSPanel:
  - senza bordi e non attivante (NSWindowStyleMaskNonactivatingPanel): non ruba il focus
  - trasparente (sfondo clearColor, opaque=False, niente ombra: l'ombra e' gia' nell'immagine)
  - ignoresMouseEvents = True: click-through come WS_EX_TRANSPARENT
  - livello sopra le finestre normali, su tutti gli Spaces e sopra le app a schermo intero
Il contenuto e' l'immagine gia' disegnata da live_panel (stili, fasi, _plan non si toccano),
convertita in NSBitmapImageRep a risoluzione piena: la misura dell'immagine in punti e'
pixel / backingScaleFactor, cosi' su Retina ogni pixel di PIL e' un pixel dello schermo.

Solo dal thread principale (quello del loop Tk), come le finestre win32 su Windows.
"""
import io

import mac_geom


class MacPanel:
    def __init__(self, log=print, click_through=True):
        self.log = log
        self.click_through = click_through
        self.panel = None
        self.view = None
        self.shown = False

    def ensure(self):
        if self.panel is not None:
            return
        import AppKit as A
        style = A.NSWindowStyleMaskBorderless | A.NSWindowStyleMaskNonactivatingPanel
        p = A.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            A.NSMakeRect(0, 0, 8, 8), style, A.NSBackingStoreBuffered, False)
        p.setOpaque_(False)
        p.setBackgroundColor_(A.NSColor.clearColor())
        p.setHasShadow_(False)
        p.setIgnoresMouseEvents_(bool(self.click_through))
        p.setLevel_(A.NSStatusWindowLevel)
        p.setCollectionBehavior_(A.NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | A.NSWindowCollectionBehaviorFullScreenAuxiliary
                                 | A.NSWindowCollectionBehaviorStationary
                                 | A.NSWindowCollectionBehaviorIgnoresCycle)
        p.setFloatingPanel_(True)
        p.setHidesOnDeactivate_(False)
        p.setBecomesKeyOnlyIfNeeded_(True)
        p.setReleasedWhenClosed_(False)
        p.setAnimationBehavior_(A.NSWindowAnimationBehaviorNone)
        v = A.NSImageView.alloc().initWithFrame_(A.NSMakeRect(0, 0, 8, 8))
        v.setImageScaling_(A.NSImageScaleAxesIndependently)
        v.setImageFrameStyle_(A.NSImageFrameNone)
        p.setContentView_(v)
        self.panel, self.view = p, v

    def _nsimage(self, img, scale):
        """PIL RGBA -> NSImage con una rappresentazione da w x h pixel e misura in punti.
        Passa per un TIFF non compresso in memoria (formato nativo di AppKit, niente
        compressione da pagare a ogni frame); PNG se il TIFF non si legge."""
        import AppKit as A
        w, h = img.size
        rep = None
        for fmt, kw in (("TIFF", {"compression": "raw"}), ("PNG", {"compress_level": 1})):
            try:
                buf = io.BytesIO()
                img.save(buf, fmt, **kw)
                raw = buf.getvalue()
                data = A.NSData.dataWithBytes_length_(raw, len(raw))
                rep = A.NSBitmapImageRep.imageRepWithData_(data)
                if rep is not None:
                    break
            except Exception:
                rep = None
        if rep is None:
            raise RuntimeError("immagine della card non convertita")
        size = A.NSMakeSize(w / scale, h / scale)
        rep.setSize_(size)
        ns = A.NSImage.alloc().initWithSize_(size)
        ns.addRepresentation_(rep)
        return ns

    def paint(self, x, y, img, screens, idx):
        """Mostra `img` con l'angolo in alto a sinistra in (x, y) pixel (contratto Wavetype),
        convertito con la scala dello schermo `idx` (quello su cui e' pianificata la card)."""
        import AppKit as A
        self.ensure()
        w, h = img.size
        sc = float(screens[idx][2]) if (screens and idx is not None) else 1.0
        fx, fy, fw, fh = mac_geom.window_frame(x, y, w, h, screens, idx)
        self.view.setImage_(self._nsimage(img.convert("RGBA") if img.mode != "RGBA" else img,
                                          sc or 1.0))
        self.panel.setFrame_display_(A.NSMakeRect(fx, fy, fw, fh), True)
        self.view.setFrame_(A.NSMakeRect(0, 0, fw, fh))

    def show(self, v):
        if self.panel is None:
            return
        if v and not self.shown:
            self.panel.orderFrontRegardless()
            self.shown = True
        elif not v and self.shown:
            self.panel.orderOut_(None)
            self.shown = False

    def destroy(self):
        try:
            if self.panel is not None:
                self.panel.orderOut_(None)
                self.panel.close()
        except Exception:
            pass
        self.panel = self.view = None
        self.shown = False
