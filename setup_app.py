#!/usr/bin/env python3
"""py2app build script for the Voicemode menu-bar application.

Build it through scripts/build-macos-app.sh rather than invoking this directly; that script
also regenerates the icon and signs the result, and an unsigned bundle loses its Microphone
and Accessibility grants on every rebuild.

This is deliberately separate from pyproject.toml. py2app wants a classic setup() call, and
keeping it out of the package metadata means `pip install` never drags py2app in.
"""

from setuptools import setup

APP = ["voiceapp.py"]

# Native extension packages that modulegraph cannot follow into, so they are copied whole.
PACKAGES = [
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "av",
    "numpy",
    "sounddevice",
    "tokenizers",
    "huggingface_hub",
    "kokoro_onnx",
    "espeakng_loader",
    "soundfile",
    # Data packages that carry a shared library. py2app would otherwise fold these into
    # python311.zip, and dlopen cannot open a library inside a zip -- it fails with errno 20
    # and the app dies at import before any window or menu appears.
    "_sounddevice_data",
    "_soundfile_data",
    "certifi",
    "objc",
    "AppKit",
    "Foundation",
]

# edge-tts is the optional online backend; it pulls babel's 33 MB of locale data into the
# bundle for a feature that sends text to Microsoft, so the application ships local TTS only.
EXCLUDES = [
    "edge_tts",
    "babel",
    # protobuf arrives with onnxruntime but is never imported during inference, and it is a
    # namespace package that py2app's imp-based lookup cannot follow anyway.
    "google",
    "google.protobuf",
    "protobuf",
    "pip",
    "setuptools",
    "py2app",
    "tkinter",
    "test",
    "unittest",
    "pydoc_data",
]

PLIST = {
    # A stable identifier is what ties the Microphone and Accessibility grants to this app.
    # Changing it makes macOS treat a rebuild as a different application and ask again.
    "CFBundleIdentifier": "com.voicemode.Voicemode",
    "CFBundleName": "Voicemode",
    "CFBundleDisplayName": "Voicemode",
    "CFBundleShortVersionString": "0.1.0",
    "CFBundleVersion": "0.1.0",
    "LSMinimumSystemVersion": "11.0",
    # No Dock icon and no menu bar of its own: this lives in the status bar.
    "LSUIElement": True,
    "NSMicrophoneUsageDescription": (
        "Voicemode records from the microphone while you dictate, and transcribes it on this "
        "Mac."
    ),
    "NSAppleEventsUsageDescription": (
        "Voicemode uses System Events to paste the transcript into the app you are using, and "
        "to copy the text you ask it to read aloud."
    ),
    "NSHumanReadableCopyright": "MIT licensed. See LICENSE.",
}

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "resources/Voicemode.icns",
    "plist": PLIST,
    "packages": PACKAGES,
    "includes": ["voice"],
    "excludes": EXCLUDES,
    # Model weights are fetched on first launch instead of being bundled; base alone is 145 MB
    # and Kokoro is another 337 MB, which would more than double the download.
    "resources": [],
    "optimize": 1,
}

setup(
    name="Voicemode",
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
