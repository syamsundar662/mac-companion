"""Draw Mac's icon (the teal orb on a dark rounded square) at every size an .iconset needs.
Usage: make_icon.py <out dir ending in .iconset>"""
import sys
from pathlib import Path

from AppKit import (NSBezierPath, NSBitmapImageFileTypePNG, NSBitmapImageRep, NSColor, NSDeviceRGBColorSpace,
                    NSGradient, NSGraphicsContext, NSMakeRect)

TEAL = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.43, 0.91, 0.85, 1)  # Mac's "ready" colour


def png(px):
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, px, px, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    s = px / 1024
    def oval(size):
        return NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect((1024 - size) / 2 * s, (1024 - size) / 2 * s, size * s, size * s))
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.07, 0.09, 0.12, 1).set()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(100 * s, 100 * s, 824 * s, 824 * s), 185 * s, 185 * s).fill()
    NSGradient.alloc().initWithColors_([TEAL.colorWithAlphaComponent_(0.5), TEAL.colorWithAlphaComponent_(0)]) \
        .drawInBezierPath_relativeCenterPosition_(oval(640), (0, 0))  # soft glow
    NSGradient.alloc().initWithColors_([NSColor.whiteColor().blendedColorWithFraction_ofColor_(0.4, TEAL), TEAL,
                                        TEAL.blendedColorWithFraction_ofColor_(0.45, NSColor.blackColor())]) \
        .drawInBezierPath_relativeCenterPosition_(oval(400), (-0.35, 0.35))  # the orb, lit from the top left
    NSGraphicsContext.restoreGraphicsState()
    return rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})


out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
for size in (16, 32, 128, 256, 512):
    png(size).writeToFile_atomically_(str(out / f"icon_{size}x{size}.png"), True)
    png(size * 2).writeToFile_atomically_(str(out / f"icon_{size}x{size}@2x.png"), True)
