#!/usr/bin/env python3
"""Render the application icon.

Draws a rounded square with the system microphone glyph on it, then hands the sizes to
iconutil. Keeping this as a script rather than committing a binary .icns means the icon can be
regenerated, and the repository stays free of build artefacts.

Usage: python scripts/make-icon.py [output.icns]
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import AppKit
from Foundation import NSMakeRect

CANVAS = 1024
# The glyph sits inside the squircle rather than filling it, which is what every other menu-bar
# application's icon does.
GLYPH_INSET = 0.26
CORNER_RADIUS = 0.2237 * CANVAS  # Matches Apple's squircle proportion closely enough.
GLYPH_COLOR = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.58, 0.22, 1.0)

# iconutil insists on exactly these names.
ICONSET_SIZES = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def render_master() -> AppKit.NSImage:
    image = AppKit.NSImage.alloc().initWithSize_((CANVAS, CANVAS))
    image.lockFocus()

    gradient = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
        AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.18, 0.20, 0.28, 1.0),
        AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.07, 0.08, 0.12, 1.0),
    )
    squircle = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(0, 0, CANVAS, CANVAS), CORNER_RADIUS, CORNER_RADIUS
    )
    gradient.drawInBezierPath_angle_(squircle, 90.0)

    glyph = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("mic.fill", "Voicemode")
    if glyph is None:
        raise SystemExit("mic.fill is unavailable; this needs macOS 11 or newer")

    config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
        CANVAS * (1 - 2 * GLYPH_INSET), AppKit.NSFontWeightRegular, 3
    )
    glyph = glyph.imageWithSymbolConfiguration_(config)
    # Tinted in a transparent layer of its own, then composited. Filling sourceAtop straight
    # onto the canvas would land on the opaque background and repaint the whole rectangle.
    # Left untinted, an SF Symbol picks up the user's accent colour instead, which would make
    # the icon differ from machine to machine.
    glyph.setTemplate_(True)
    size = glyph.size()

    tinted = AppKit.NSImage.alloc().initWithSize_(size)
    tinted.lockFocus()
    layer_rect = NSMakeRect(0, 0, size.width, size.height)
    glyph.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
        layer_rect,
        AppKit.NSZeroRect,
        AppKit.NSCompositingOperationSourceOver,
        1.0,
        True,
        {AppKit.NSImageHintInterpolation: AppKit.NSImageInterpolationHigh},
    )
    GLYPH_COLOR.set()
    AppKit.NSRectFillUsingOperation(layer_rect, AppKit.NSCompositingOperationSourceAtop)
    tinted.unlockFocus()

    tinted.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(
            (CANVAS - size.width) / 2, (CANVAS - size.height) / 2, size.width, size.height
        ),
        AppKit.NSZeroRect,
        AppKit.NSCompositingOperationSourceOver,
        1.0,
    )

    image.unlockFocus()
    return image


def write_png(image: AppKit.NSImage, size: int, path: Path) -> None:
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, AppKit.NSCalibratedRGBColorSpace, 0, 0
    )
    context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(context)
    image.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, size, size),
        AppKit.NSZeroRect,
        AppKit.NSCompositingOperationCopy,
        1.0,
    )
    AppKit.NSGraphicsContext.restoreGraphicsState()

    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    path.write_bytes(bytes(data))


def main() -> int:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "resources/Voicemode.icns")
    output.parent.mkdir(parents=True, exist_ok=True)

    master = render_master()
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Voicemode.iconset"
        iconset.mkdir()
        for name, size in ICONSET_SIZES:
            write_png(master, size, iconset / name)
        subprocess.run(
            ["iconutil", "--convert", "icns", str(iconset), "--output", str(output)],
            check=True,
        )

    print(f"wrote {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
