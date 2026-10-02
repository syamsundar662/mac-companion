"""Screen glow: Mac's take on the Siri edge light.

An aurora ring (teal, sky, violet, rose, the same colours as the orb) slowly turns around the edge of the screen while
Mac is listening, swells with your voice, becomes a calm thin frame while Mac is using your screen (so you always know
when it's in control), and turns amber when it needs your OK.

Built with Core Animation: the gradient is drawn once and only rotated by the GPU, so it costs next to nothing.
The window is click-through and hidden from screenshots, so Mac's own screen reading never sees it.
"""
import math

import Quartz
from AppKit import (
    NSBackingStoreBuffered, NSColor, NSPanel, NSScreenSaverWindowLevel, NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle, NSWindowCollectionBehaviorStationary, NSWindowSharingNone,
    NSWindowSharingReadOnly, NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
)
from PyObjCTools import AppHelper


def _rgb(r, g, b):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, 1).CGColor()


# Deeper, more saturated takes on the orb's colours: light has to read as light over any wallpaper or app.
TEAL, BLUE, VIOLET, PINK = (0, 226, 196), (40, 120, 255), (150, 78, 255), (255, 56, 146)
AMBER, CORAL, GOLD = (255, 166, 36), (255, 92, 92), (255, 206, 84)
PALETTES = {
    "aurora": [TEAL, BLUE, VIOLET, PINK, TEAL],
    "calm": [TEAL, BLUE, TEAL, BLUE, TEAL],
    "amber": [AMBER, CORAL, GOLD, AMBER, AMBER],
}
# mode: (palette, brightness, glow width)
MODES = {
    "listening": ("aurora", 1.0, 1.0),
    "thinking": ("aurora", 0.85, 0.75),
    "working": ("calm", 0.55, 0.5),    # Mac is using your screen: a quiet frame, not a light show
    "approval": ("amber", 0.95, 0.8),
}
# The soft edge is a few stacked strokes centred on the screen edge, fading outwards: (width in points, opacity)
STROKES = [(76, 0.09), (46, 0.2), (24, 0.42), (11, 0.75), (4, 1.0)]  # a crisp bright edge with a short, strong falloff
TURN_S = 7.0
CORNER = 28.0  # points


class Glow:
    def __init__(self, share_in_screenshots=False):
        self.share = share_in_screenshots  # only for testing: normally it's kept out of screenshots
        self.window = None
        self.screen_id = None
        self.mode = None
        self.width_scale = 1.0
        self.gen = 0

    # ---------- building ----------

    def _build(self, screen):
        frame = screen.frame()
        w = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel, NSBackingStoreBuffered, False)
        w.setLevel_(NSScreenSaverWindowLevel)  # above the menu bar and full-screen apps
        w.setOpaque_(False)
        w.setBackgroundColor_(NSColor.clearColor())
        w.setHasShadow_(False)
        w.setIgnoresMouseEvents_(True)
        w.setHidesOnDeactivate_(False)
        w.setSharingType_(NSWindowSharingReadOnly if self.share else NSWindowSharingNone)
        w.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
                                 | NSWindowCollectionBehaviorStationary | NSWindowCollectionBehaviorIgnoresCycle)
        view = NSView.alloc().initWithFrame_(((0, 0), frame.size))
        view.setWantsLayer_(True)
        w.setContentView_(view)

        width, height = frame.size.width, frame.size.height
        scale = screen.backingScaleFactor()
        corner = CORNER  # rounded on every screen, so the light curves around the corners instead of meeting square

        container = Quartz.CALayer.layer()
        container.setFrame_(((0, 0), (width, height)))
        container.setOpacity_(0.0)

        side = math.hypot(width, height)  # big enough to cover the screen at any angle while it turns
        gradient = Quartz.CAGradientLayer.layer()
        gradient.setType_(Quartz.kCAGradientLayerConic)
        gradient.setFrame_((((width - side) / 2, (height - side) / 2), (side, side)))
        gradient.setStartPoint_((0.5, 0.5))
        gradient.setEndPoint_((0.5, 1.0))
        gradient.setContentsScale_(scale)
        spin = Quartz.CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
        spin.setFromValue_(0.0)
        spin.setToValue_(-2 * math.pi)
        spin.setDuration_(TURN_S)
        spin.setRepeatCount_(float("inf"))
        gradient.addAnimation_forKey_(spin, "spin")
        container.addSublayer_(gradient)

        mask = Quartz.CALayer.layer()
        mask.setFrame_(((0, 0), (width, height)))
        path = Quartz.CGPathCreateWithRoundedRect(((0, 0), (width, height)), corner, corner, None)
        self.strokes = []
        for stroke_width, alpha in STROKES:
            s = Quartz.CAShapeLayer.layer()
            s.setFrame_(((0, 0), (width, height)))
            s.setPath_(path)
            s.setFillColor_(None)
            s.setStrokeColor_(NSColor.colorWithWhite_alpha_(1.0, alpha).CGColor())
            s.setLineWidth_(stroke_width)
            s.setContentsScale_(scale)
            mask.addSublayer_(s)
            self.strokes.append((s, stroke_width))
        container.setMask_(mask)
        view.layer().addSublayer_(container)

        self.window, self.container, self.gradient = w, container, gradient

    def _screen(self):
        from AppKit import NSEvent, NSScreen
        m = NSEvent.mouseLocation()
        for s in NSScreen.screens():
            f = s.frame()
            if f.origin.x <= m.x < f.origin.x + f.size.width and f.origin.y <= m.y < f.origin.y + f.size.height:
                return s
        return NSScreen.mainScreen()

    # ---------- showing ----------

    def show(self, mode):
        """listening / thinking / working / approval"""
        if mode not in MODES or mode == self.mode:
            return
        screen = self._screen()
        screen_id = screen.deviceDescription()["NSScreenNumber"]
        if self.window is None or screen_id != self.screen_id or self.window.frame() != screen.frame():
            if self.window is not None:
                self.window.orderOut_(None)
            self._build(screen)
            self.screen_id = screen_id
        self.mode = mode
        self.gen += 1
        palette, brightness, width = MODES[mode]
        self.width_scale = width
        self.window.orderFrontRegardless()
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.45)
        self.gradient.setColors_([_rgb(*c) for c in PALETTES[palette]])
        self.container.setOpacity_(brightness)
        for layer, base in self.strokes:
            layer.setLineWidth_(base * width)
        Quartz.CATransaction.commit()

    def level(self, x):
        """Your voice (0 to 1) swells the glow while listening."""
        if self.mode != "listening" or self.window is None:
            return
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.12)
        for layer, base in self.strokes:
            layer.setLineWidth_(base * self.width_scale * (1.0 + 0.9 * x))
        self.container.setOpacity_(0.8 + 0.2 * x)
        Quartz.CATransaction.commit()

    def hide(self):
        if self.mode is None or self.window is None:
            return
        self.mode = None
        self.gen += 1
        gen = self.gen
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.6)
        self.container.setOpacity_(0.0)
        Quartz.CATransaction.commit()
        AppHelper.callLater(0.65, lambda: gen == self.gen and self.window.orderOut_(None))
