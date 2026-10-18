#!/usr/bin/env python3
"""Menu-bar front end for voicemode on macOS.

This registers its own global hotkeys through Carbon's RegisterEventHotKey, which needs no
permission at all, so the bundled application needs neither skhd nor a launchd agent. The two
grants macOS does require are still required: Microphone to record, and Accessibility to put
the transcript into another application. Both are prompted for normally here, because this
runs as a real bundle with a stable identity rather than as a daemon the user has to add to a
list by hand.

The transcription itself is voice.py's; this module only drives it and owns the UI.
"""

from __future__ import annotations

import argparse
import ctypes
import locale
import os
import subprocess
import sys
import threading
import time
import traceback
from ctypes import POINTER, Structure, byref, c_int, c_uint32, c_void_p
from pathlib import Path
from typing import Callable, Optional

import AppKit
import objc
from Foundation import NSObject, NSOperationQueue, NSUserDefaults

import voice


def _force_utf8_locale() -> None:
    """Move the process off the US-ASCII locale an app bundle starts with.

    LaunchServices gives the bundle no usable locale, so locale.getencoding() answers
    US-ASCII and UTF-8 mode is off. That is the default for open() and for subprocess text
    mode, so anything that does not name an encoding breaks on the first non-ASCII character
    -- which for a transcription tool is most languages. PYTHONUTF8 via LSEnvironment is
    ignored on current macOS, and sys.flags.utf8_mode cannot be changed once running, so the
    locale itself is what has to move.
    """

    if locale.getencoding().lower() in ("utf-8", "utf8"):
        return
    for candidate in ("en_US.UTF-8", "C.UTF-8", "UTF-8"):
        try:
            locale.setlocale(locale.LC_ALL, candidate)
        except locale.Error:
            continue
        if locale.getencoding().lower() in ("utf-8", "utf8"):
            return


_force_utf8_locale()

BUNDLE_NAME = "Voicemode"
MODEL_CHOICES = ("tiny", "base", "small", "medium")
DEFAULT_MODEL = "small"

# Whisper knows 100 languages; these are the ones offered in the menu. Pinning one is both
# faster and safer than autodetection, which can latch onto the wrong language when handed a
# quiet or very short recording. An empty code means autodetect.
LANGUAGE_CHOICES = (
    ("Auto-detect", ""),
    ("English", "en"),
    ("Vietnamese", "vi"),
    ("Chinese", "zh"),
    ("French", "fr"),
    ("German", "de"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Spanish", "es"),
)
DEFAULT_LANGUAGE = "en"

# SF Symbols, which need macOS 11 or newer. The menu bar shows the state at a glance so the
# user never has to wonder whether a toggle registered.
ICON_IDLE = "mic"
ICON_RECORDING = "record.circle.fill"
ICON_BUSY = "waveform"
ICON_DOWNLOADING = "arrow.down.circle"


# --- Carbon global hotkeys -------------------------------------------------------------------

CARBON = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/Carbon.framework/Versions/Current/Carbon"
)

kEventClassKeyboard = 0x6B657962  # 'keyb'
kEventHotKeyPressed = 5
kEventParamDirectObject = 0x2D2D2D2D  # '----'
typeEventHotKeyID = 0x686B6964  # 'hkid'
HOTKEY_SIGNATURE = 0x766F6963  # 'voic'

cmdKey = 0x0100
optionKey = 0x0800
kVK_ANSI_B = 11
kVK_ANSI_T = 17

# eventHotKeyExistsErr: another application already owns that combination.
EVENT_HOTKEY_EXISTS_ERR = -9878


class EventTypeSpec(Structure):
    _fields_ = [("eventClass", c_uint32), ("eventKind", c_uint32)]


class EventHotKeyID(Structure):
    _fields_ = [("signature", c_uint32), ("id", c_uint32)]


EventHandlerProcPtr = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p, c_void_p)

CARBON.GetApplicationEventTarget.restype = c_void_p
CARBON.RegisterEventHotKey.argtypes = [
    c_uint32,
    c_uint32,
    EventHotKeyID,
    c_void_p,
    c_uint32,
    POINTER(c_void_p),
]
CARBON.RegisterEventHotKey.restype = c_int
CARBON.UnregisterEventHotKey.argtypes = [c_void_p]
CARBON.UnregisterEventHotKey.restype = c_int
CARBON.InstallEventHandler.argtypes = [
    c_void_p,
    EventHandlerProcPtr,
    c_uint32,
    POINTER(EventTypeSpec),
    c_void_p,
    POINTER(c_void_p),
]
CARBON.InstallEventHandler.restype = c_int
CARBON.GetEventParameter.argtypes = [
    c_void_p,
    c_uint32,
    c_uint32,
    POINTER(c_uint32),
    c_uint32,
    POINTER(c_uint32),
    c_void_p,
]
CARBON.GetEventParameter.restype = c_int


class HotKeyManager:
    """Holds the registered Carbon hotkeys and routes presses to Python callables."""

    def __init__(self) -> None:
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._refs: list[c_void_p] = []
        self._next_id = 1
        # The trampoline has to stay referenced for as long as Carbon may call it; a local
        # would be collected and the first keypress would jump into freed memory.
        self._handler = EventHandlerProcPtr(self._dispatch)
        self._handler_ref = c_void_p()
        self._installed = False

    def _dispatch(self, next_handler: c_void_p, event: c_void_p, user_data: c_void_p) -> int:
        hotkey = EventHotKeyID()
        actual_type = c_uint32()
        actual_size = c_uint32()
        status = CARBON.GetEventParameter(
            event,
            kEventParamDirectObject,
            typeEventHotKeyID,
            byref(actual_type),
            ctypes.sizeof(hotkey),
            byref(actual_size),
            byref(hotkey),
        )
        if status != 0:
            return status
        callback = self._callbacks.get(hotkey.id)
        if callback is not None:
            callback()
        return 0

    def _install_handler(self) -> None:
        spec = EventTypeSpec(kEventClassKeyboard, kEventHotKeyPressed)
        status = CARBON.InstallEventHandler(
            CARBON.GetApplicationEventTarget(),
            self._handler,
            1,
            byref(spec),
            None,
            byref(self._handler_ref),
        )
        if status != 0:
            raise RuntimeError(f"InstallEventHandler failed with status {status}")
        self._installed = True

    def register(self, keycode: int, modifiers: int, callback: Callable[[], None]) -> bool:
        """Claim one hotkey. False means another application already holds it."""

        if not self._installed:
            self._install_handler()

        hotkey_id = self._next_id
        ref = c_void_p()
        status = CARBON.RegisterEventHotKey(
            keycode,
            modifiers,
            EventHotKeyID(HOTKEY_SIGNATURE, hotkey_id),
            CARBON.GetApplicationEventTarget(),
            0,
            byref(ref),
        )
        if status == EVENT_HOTKEY_EXISTS_ERR:
            return False
        if status != 0:
            raise RuntimeError(f"RegisterEventHotKey failed with status {status}")

        self._callbacks[hotkey_id] = callback
        self._refs.append(ref)
        self._next_id += 1
        return True

    def unregister_all(self) -> None:
        for ref in self._refs:
            CARBON.UnregisterEventHotKey(ref)
        self._refs.clear()
        self._callbacks.clear()


# --- helpers ---------------------------------------------------------------------------------


APP_LOG = voice.STATE_DIR / "voiceapp.log"


def log(message: str) -> None:
    """Append a line to the app's log.

    A menu-bar app has nowhere to print: stdout goes nowhere under LaunchServices and a
    notification is gone as soon as it is dismissed. Without this, diagnosing a failure means
    guessing from the outside.
    """

    try:
        APP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with APP_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def on_main_thread(callable_: Callable[[], None]) -> None:
    """Run a callable on the main thread, which is the only one allowed to touch the UI."""

    NSOperationQueue.mainQueue().addOperationWithBlock_(callable_)


def skhd_bindings_active() -> bool:
    """Whether the CLI's skhd shortcuts are installed and the daemon is running.

    Both would answer the same keystroke: skhd would spawn `voicemode --toggle` while this app
    starts its own recording, giving two recorders and two transcripts from one press.
    """

    try:
        config = voice.SKHD_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return False
    if voice.SKHD_BEGIN not in config:
        return False
    try:
        return subprocess.run(["pgrep", "-x", "skhd"], capture_output=True).returncode == 0
    except OSError:
        return False


def build_args(model: str, language: str) -> argparse.Namespace:
    """Build voice.py's argument namespace without letting it see our own argv.

    py2app passes bundle arguments such as -psn_0_… which argparse would reject.
    """

    saved = sys.argv
    sys.argv = ["voicemode"]
    try:
        args = voice.parse_args()
    finally:
        sys.argv = saved

    args.model = model
    # voice.py treats None as "detect it"; the menu stores that as an empty string.
    args.language = language or None
    # An application has to be able to fetch its own model; there is no terminal to tell.
    args.allow_download = True
    # Beeps are the only feedback while a recording is in progress and the menu bar is hidden
    # behind a full-screen window, so they stay on.
    args.beep = True
    return args


# --- application -----------------------------------------------------------------------------


class VoicemodeApp(NSObject):
    def init(self):
        self = objc.super(VoicemodeApp, self).init()
        if self is None:
            return None

        self.hotkeys = HotKeyManager()
        self.recorder: Optional[voice.Recorder] = None
        self.model_state = None
        self.state = "starting"
        self.last_transcript = ""
        self._lock = threading.Lock()
        return self

    # -- preferences, stored where a macOS application is expected to store them

    def _defaults(self):
        return NSUserDefaults.standardUserDefaults()

    @property
    def model(self) -> str:
        return self._defaults().stringForKey_("model") or DEFAULT_MODEL

    @property
    def language(self) -> str:
        defaults = self._defaults()
        stored = defaults.stringForKey_("language")
        if stored is not None:
            return stored
        # Migrate the old boolean preference so an existing install keeps its behaviour.
        if defaults.objectForKey_("englishOnly") is not None:
            return "en" if defaults.boolForKey_("englishOnly") else ""
        return DEFAULT_LANGUAGE

    def current_args(self) -> argparse.Namespace:
        return build_args(self.model, self.language)

    # -- lifecycle

    def applicationDidFinishLaunching_(self, notification) -> None:
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        log(f"--- launched: model={self.model} language={self.language or 'auto'} ---")
        # Recorded because the locale inside an app bundle is not the one a terminal gets, and
        # an ASCII result here is what broke non-English transcripts before encodings were
        # pinned explicitly on the subprocess calls.
        log(
            f"locale encoding={locale.getencoding()} utf8_mode={sys.flags.utf8_mode} "
            f"filesystem={sys.getfilesystemencoding()}"
        )
        self._build_status_item()
        self._register_hotkeys()
        if skhd_bindings_active():
            voice.notify(
                BUNDLE_NAME,
                "The skhd shortcuts from the command-line setup are also running; one press "
                "would start two recordings. Remove the voicemode block from ~/.skhdrc.",
                self.current_args(),
            )
        self.set_state("preparing")
        threading.Thread(target=self._preload_model, daemon=True).start()

    def applicationWillTerminate_(self, notification) -> None:
        self.hotkeys.unregister_all()

    def _build_status_item(self) -> None:
        bar = AppKit.NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)

        menu = AppKit.NSMenu.alloc().init()

        self.status_line = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Starting…", None, ""
        )
        self.status_line.setEnabled_(False)
        menu.addItem_(self.status_line)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        self.dictate_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Start Dictation", "toggleDictation:", ""
        )
        self.dictate_item.setTarget_(self)
        menu.addItem_(self.dictate_item)

        speak_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Speak Selection", "speakSelection:", ""
        )
        speak_item.setTarget_(self)
        menu.addItem_(speak_item)

        self.copy_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Copy Last Transcript", "copyLast:", ""
        )
        self.copy_item.setTarget_(self)
        self.copy_item.setEnabled_(False)
        menu.addItem_(self.copy_item)

        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        model_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Model", None, ""
        )
        model_menu = AppKit.NSMenu.alloc().init()
        self.model_items = {}
        for name in MODEL_CHOICES:
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                name, "selectModel:", ""
            )
            item.setTarget_(self)
            item.setRepresentedObject_(name)
            model_menu.addItem_(item)
            self.model_items[name] = item
        model_item.setSubmenu_(model_menu)
        menu.addItem_(model_item)

        language_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Language", None, ""
        )
        language_menu = AppKit.NSMenu.alloc().init()
        self.language_items = {}
        for label, code in LANGUAGE_CHOICES:
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, "selectLanguage:", ""
            )
            item.setTarget_(self)
            item.setRepresentedObject_(code)
            language_menu.addItem_(item)
            self.language_items[code] = item
        language_item.setSubmenu_(language_menu)
        menu.addItem_(language_item)

        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        log_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Open Log", "openLog:", ""
        )
        log_item.setTarget_(self)
        menu.addItem_(log_item)

        quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"Quit {BUNDLE_NAME}", "terminate:", "q"
        )
        quit_item.setTarget_(AppKit.NSApp)
        menu.addItem_(quit_item)

        self.status_item.setMenu_(menu)
        self._sync_pref_checkmarks()
        self._set_icon(ICON_IDLE)

    def _sync_pref_checkmarks(self) -> None:
        for name, item in self.model_items.items():
            item.setState_(AppKit.NSControlStateValueOn if name == self.model else AppKit.NSControlStateValueOff)
        current = self.language
        for code, item in self.language_items.items():
            item.setState_(
                AppKit.NSControlStateValueOn if code == current else AppKit.NSControlStateValueOff
            )

    def _set_icon(self, symbol: str) -> None:
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            symbol, BUNDLE_NAME
        )
        if image is None:
            # Older macOS without that symbol: fall back to a short text label.
            self.status_item.button().setTitle_("🎙")
            return
        image.setTemplate_(True)
        self.status_item.button().setImage_(image)

    def _register_hotkeys(self) -> None:
        taken = []
        if not self.hotkeys.register(kVK_ANSI_B, cmdKey | optionKey, self._hotkey_dictate):
            taken.append("⌘⌥B")
        if not self.hotkeys.register(kVK_ANSI_T, cmdKey | optionKey, self._hotkey_speak):
            taken.append("⌘⌥T")
        if taken:
            # Carbon refuses a combination another application already owns, and silently
            # doing nothing on keypress is the worst possible failure, so say so.
            voice.notify(
                BUNDLE_NAME,
                f"Could not claim {', '.join(taken)}; another app already uses it.",
                self.current_args(),
            )

    # -- hotkey entry points, called on the main thread by Carbon

    def _hotkey_dictate(self) -> None:
        self.toggleDictation_(None)

    def _hotkey_speak(self) -> None:
        self.speakSelection_(None)

    # -- state

    def set_state(self, state: str, detail: str = "") -> None:
        def apply() -> None:
            self.state = state
            labels = {
                "preparing": ("Preparing model…", ICON_DOWNLOADING, "Start Dictation", False),
                "idle": ("Ready", ICON_IDLE, "Start Dictation", True),
                "recording": ("Recording…", ICON_RECORDING, "Stop and Transcribe", True),
                "transcribing": ("Transcribing…", ICON_BUSY, "Transcribing…", False),
                "speaking": ("Speaking…", ICON_BUSY, "Start Dictation", True),
            }
            title, icon, action_title, enabled = labels.get(
                state, ("Ready", ICON_IDLE, "Start Dictation", True)
            )
            self.status_line.setTitle_(detail or title)
            self.dictate_item.setTitle_(action_title)
            self.dictate_item.setEnabled_(enabled)
            self.copy_item.setEnabled_(bool(self.last_transcript))
            self._set_icon(icon)

        on_main_thread(apply)

    # -- model

    def _preload_model(self) -> None:
        """Load (downloading if needed) the model up front, so the first dictation is quick."""

        args = self.current_args()
        try:
            self.model_state = voice.load_model(args)
        except Exception as exc:
            self.set_state("idle", f"Model failed: {exc}")
            voice.notify(BUNDLE_NAME, f"Could not load the {args.model} model: {exc}", args)
            return
        self.set_state("idle")

    def _ensure_model(self, args: argparse.Namespace):
        with self._lock:
            if self.model_state is None or self.model_state.model is None:
                self.model_state = voice.load_model(args)
            return self.model_state

    # -- actions

    def toggleDictation_(self, sender) -> None:
        if self.state in ("preparing", "transcribing"):
            return
        if self.recorder is not None:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        args = self.current_args()
        try:
            device = voice.parse_device(args.input_device)
            sample_rate = voice.resolve_sample_rate(device, args.sample_rate)
            recorder = voice.Recorder(sample_rate=sample_rate, input_device=device)
            voice.play_cue(args, "start")
            recorder.start()
        except Exception as exc:
            # The usual cause is a denied Microphone grant, which surfaces here as a
            # PortAudio error rather than anything mentioning permissions.
            self.set_state("idle", f"Could not record: {exc}")
            voice.notify(
                BUNDLE_NAME,
                f"Could not start recording: {exc}. Check Microphone access for {BUNDLE_NAME}.",
                args,
            )
            return

        self.recorder = recorder
        self.set_state("recording")

    def _stop_recording(self) -> None:
        recorder = self.recorder
        self.recorder = None
        if recorder is None:
            return

        args = self.current_args()
        self.set_state("transcribing")

        def work() -> None:
            wav_path = None
            should_delete = False
            try:
                voice.play_cue(args, "stop")
                wav_path, duration, should_delete = recorder.stop_to_wav(None)
                if duration < 0.3:
                    # Whisper invents fluent text for near-silence, so do not ask it to.
                    self.set_state("idle", "Too short, ignored")
                    return
                state = self._ensure_model(args)
                text, state = voice.transcribe(state, wav_path, args)
                self.model_state = state
                if not text:
                    self.set_state("idle", "Nothing recognised")
                    return
                self.last_transcript = text
                log(f"transcribed ({duration:.1f}s, {args.language or 'auto'}): {text}")
                inserted = voice.insert_text(text, args)
                if not inserted:
                    voice.copy_to_clipboard(text)
                    self.set_state("idle", "Copied to clipboard")
                    voice.notify(
                        BUNDLE_NAME,
                        f"Could not insert text, so it was copied instead. Check Accessibility "
                        f"access for {BUNDLE_NAME}.",
                        args,
                    )
                    return
                self.set_state("idle", f"Inserted {len(text.split())} words")
            except Exception as exc:
                log(f"transcription failed: {exc!r}\n{traceback.format_exc()}")
                self.set_state("idle", f"Failed: {exc}")
                voice.notify(BUNDLE_NAME, f"Transcription failed: {exc}", args)
            finally:
                if should_delete and wav_path is not None:
                    try:
                        wav_path.unlink()
                    except OSError:
                        pass

        threading.Thread(target=work, daemon=True).start()

    def speakSelection_(self, sender) -> None:
        args = self.current_args()

        def work() -> None:
            try:
                if voice.stop_tts(args, quiet=True):
                    self.set_state("idle")
                    return
                text, source = voice.selected_or_clipboard_text(
                    getattr(args, "macos_copy_selection", True)
                )
                if not text:
                    self.set_state("idle", "Nothing selected")
                    return
                self.set_state("speaking", f"Speaking {source}…")
                voice.start_tts_background(text, args, source)
                self.set_state("idle")
            except Exception as exc:
                self.set_state("idle", f"Speech failed: {exc}")
                voice.notify(BUNDLE_NAME, f"Could not speak the selection: {exc}", args)

        threading.Thread(target=work, daemon=True).start()

    def copyLast_(self, sender) -> None:
        if self.last_transcript:
            voice.copy_to_clipboard(self.last_transcript)
            self.set_state("idle", "Copied")

    def selectModel_(self, sender) -> None:
        name = sender.representedObject()
        if name == self.model:
            return
        self._defaults().setObject_forKey_(name, "model")
        self.model_state = None
        self._sync_pref_checkmarks()
        self.set_state("preparing")
        threading.Thread(target=self._preload_model, daemon=True).start()

    def openLog_(self, sender) -> None:
        log("log opened from the menu")
        AppKit.NSWorkspace.sharedWorkspace().openFile_(str(APP_LOG))

    def selectLanguage_(self, sender) -> None:
        code = sender.representedObject()
        if code == self.language:
            return
        self._defaults().setObject_forKey_(code, "language")
        self._sync_pref_checkmarks()
        label = dict((c, l) for l, c in LANGUAGE_CHOICES).get(code, code or "Auto-detect")
        self.set_state("idle", f"Language: {label}")


def main() -> int:
    # voice.py's state directory is normally created by the CLI; the app may be the first to run.
    voice.STATE_DIR.mkdir(parents=True, exist_ok=True)

    app = AppKit.NSApplication.sharedApplication()
    delegate = VoicemodeApp.alloc().init()
    app.setDelegate_(delegate)
    # Keep the delegate alive for the life of the process; NSApplication does not retain it.
    objc.setAssociatedObject(app, b"voicemode_delegate", delegate, objc.OBJC_ASSOCIATION_RETAIN)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
