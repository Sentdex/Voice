#!/usr/bin/env python3.11
"""Desktop-global voice command for STT and TTS.

Press Super+B to start recording, press Super+B again to stop and transcribe.
Press Super+T to speak selected text, press Super+T again to stop speaking.
Ctrl+C exits.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import ctypes
import ctypes.util
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tty
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel


CTRL_B = "\x02"
APP_NAME = "Voice STT"
STATE_DIR = Path(os.getenv("XDG_RUNTIME_DIR", f"/tmp/voice-stt-{os.getuid()}")) / "voice-stt"
PID_FILE = STATE_DIR / "recorder.pid"
TTS_PID_FILE = STATE_DIR / "tts.pid"
LOG_FILE = STATE_DIR / "voice.log"
TTS_LOG_FILE = STATE_DIR / "tts.log"
GNOME_BINDING_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/voice-stt/"
GNOME_TTS_BINDING_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/voice-tts/"
GNOME_BINDING_SCHEMA = (
    "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:"
    f"{GNOME_BINDING_PATH}"
)
GNOME_TTS_BINDING_SCHEMA = (
    "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:"
    f"{GNOME_TTS_BINDING_PATH}"
)
DEFAULT_TTS_VOICE = "en-GB-RyanNeural"
DEFAULT_TTS_SPEED = 2.0
DEFAULT_TTS_BACKEND = "kokoro"
DEFAULT_KOKORO_VOICE = "af_heart"
SECONDARY_KOKORO_VOICE = "bm_george"
DEFAULT_KOKORO_LANG = "en-us"

IS_MACOS = sys.platform == "darwin"
SKHD_CONFIG = Path.home() / ".skhdrc"
SKHD_BEGIN = "# >>> voicemode hotkeys >>>"
SKHD_END = "# <<< voicemode hotkeys <<<"
# skhd spells the Option key "alt" in its config, but nobody's keyboard does, so the
# bindings carry a separate label for anything a person reads.
MACOS_STT_BINDING = "cmd + alt - b"
MACOS_TTS_BINDING = "cmd + alt - t"
MACOS_STT_LABEL = "Cmd+Option+B (⌘⌥B)"
MACOS_TTS_LABEL = "Cmd+Option+T (⌘⌥T)"
MACOS_ACCESSIBILITY_HINT = (
    "macOS refused the synthetic keystroke. Grant Accessibility to the process that launches "
    "voicemode (skhd for hotkeys, or Terminal/iTerm when running by hand) under "
    "System Settings > Privacy & Security > Accessibility, then try again."
)
# Homebrew prefixes differ by architecture, so both are probed when the espeak-ng
# library has to be located by hand.
MACOS_ESPEAK_LIB_DIRS = ("/opt/homebrew/lib", "/usr/local/lib")
MACOS_ESPEAK_LIB_NAMES = ("libespeak-ng.dylib", "libespeak-ng.1.dylib")
MACOS_ESPEAK_HINT = (
    "No usable espeak-ng library was found. kokoro-onnx normally ships one, so install the "
    "system library with 'brew install espeak-ng' or point PHONEMIZER_ESPEAK_LIBRARY at a "
    "libespeak-ng.dylib."
)
MACOS_AUTOMATION_HINT = (
    "macOS refused the Apple event. Allow the launching process to control System Events under "
    "System Settings > Privacy & Security > Automation."
)
MACOS_NOTIFY_SCRIPT = """on run argv
display notification (item 2 of argv) with title (item 1 of argv)
end run"""
MACOS_KEYSTROKE_SCRIPT = """on run argv
tell application "System Events" to keystroke (item 1 of argv)
end run"""
MACOS_COMMAND_KEY_SCRIPT = """on run argv
tell application "System Events" to keystroke (item 1 of argv) using command down
end run"""
MACOS_ACCESSIBILITY_PROBE = 'tell application "System Events" to return UI elements enabled'


@dataclass
class ModelState:
    model: WhisperModel
    requested_device: str
    requested_compute_type: str


@dataclass
class TtsDefaults:
    voice: str
    speed: float


class TerminalKeys:
    def __enter__(self) -> "TerminalKeys":
        if not sys.stdin.isatty():
            raise RuntimeError("voice needs an interactive terminal for Ctrl+B")
        self._fd = sys.stdin.fileno()
        self._old_attrs = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_attrs)

    def read_key(self, timeout: float = 0.1) -> Optional[str]:
        readable, _, _ = select.select([sys.stdin], [], [], timeout)
        if not readable:
            return None
        return sys.stdin.read(1)


class Recorder:
    def __init__(self, sample_rate: int, input_device: Optional[int | str]) -> None:
        self.sample_rate = sample_rate
        self.input_device = input_device
        self.frames: list[np.ndarray] = []
        self.statuses: list[str] = []
        self.stream: Optional[sd.InputStream] = None
        self.started_at: Optional[float] = None

    @property
    def recording(self) -> bool:
        return self.stream is not None

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            self.statuses.append(str(status))
        self.frames.append(indata.copy())

    def start(self) -> None:
        self.frames = []
        self.statuses = []
        self.stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=self.input_device,
            callback=self._callback,
        )
        self.stream.start()
        self.started_at = time.monotonic()

    def stop_to_wav(self, save_dir: Optional[Path]) -> tuple[Path, float, bool]:
        if self.stream is None or self.started_at is None:
            raise RuntimeError("recording is not active")

        stream = self.stream
        self.stream = None
        stream.stop()
        stream.close()

        duration = time.monotonic() - self.started_at
        self.started_at = None

        if not self.frames:
            raise RuntimeError("no audio frames were captured")

        audio = np.concatenate(self.frames, axis=0)
        audio = np.clip(audio, -1.0, 1.0)
        pcm = (audio * 32767.0).astype(np.int16)

        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)
            wav_path = save_dir / time.strftime("voice-%Y%m%d-%H%M%S.wav")
            should_delete = False
        else:
            temp = tempfile.NamedTemporaryFile(prefix="voice-", suffix=".wav", delete=False)
            wav_path = Path(temp.name)
            temp.close()
            should_delete = True

        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            wav.writeframes(pcm.tobytes())

        return wav_path, duration, should_delete


def parse_device(value: Optional[str]) -> Optional[int | str]:
    if value is None or value == "":
        return None
    return int(value) if value.isdigit() else value


def resolve_sample_rate(input_device: Optional[int | str], override: Optional[int]) -> int:
    if override:
        return override
    try:
        info = sd.query_devices(input_device, "input") if input_device is not None else sd.query_devices(kind="input")
        return int(info["default_samplerate"])
    except Exception:
        return 16000


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def load_hermes_tts_defaults() -> TtsDefaults:
    defaults = TtsDefaults(voice=DEFAULT_TTS_VOICE, speed=DEFAULT_TTS_SPEED)
    config_path = Path(os.getenv("HERMES_CONFIG", "~/.hermes/config.yaml")).expanduser()
    if not config_path.exists():
        return defaults

    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        tts_config = data.get("tts", {}) if isinstance(data, dict) else {}
        edge_config = tts_config.get("edge", {}) if isinstance(tts_config, dict) else {}
        voice = edge_config.get("voice") or defaults.voice
        speed = edge_config.get("speed", tts_config.get("speed", defaults.speed))
        return TtsDefaults(voice=str(voice), speed=float(speed))
    except Exception:
        return defaults


def edge_rate_from_speed(speed: float) -> Optional[str]:
    if speed == 1.0:
        return None
    pct = round((speed - 1.0) * 100)
    return f"{pct:+d}%"


def voice_root() -> Path:
    return Path(__file__).resolve().parent


def default_kokoro_model_path() -> Path:
    return voice_root() / "models" / "kokoro" / "kokoro-v1.0.onnx"


def default_kokoro_voices_path() -> Path:
    return voice_root() / "models" / "kokoro" / "voices-v1.0.bin"


def normalize_tts_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized.replace("—", ", ").replace("’", "'")


def play_tone(args: argparse.Namespace, frequency: float, duration: float) -> None:
    if not args.beep or frequency <= 0 or duration <= 0:
        return

    sample_rate = 44100
    frames = max(1, int(sample_rate * duration))
    timeline = np.arange(frames, dtype=np.float32) / sample_rate
    samples = np.sin(2 * np.pi * frequency * timeline).astype(np.float32)

    fade_frames = min(frames // 2, int(sample_rate * 0.01))
    if fade_frames > 0:
        fade = np.linspace(0.0, 1.0, fade_frames, dtype=np.float32)
        samples[:fade_frames] *= fade
        samples[-fade_frames:] *= fade[::-1]

    volume = max(0.0, min(float(args.beep_volume), 1.0))
    audio = (samples * volume).reshape(-1, 1)
    output_device = parse_device(args.beep_output_device)

    try:
        with sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            device=output_device,
        ) as stream:
            stream.write(audio)
    except Exception as exc:
        print(f"Audio cue failed: {exc}", file=sys.stderr, flush=True)


def play_cue(args: argparse.Namespace, cue: str) -> None:
    if not args.beep:
        return

    patterns = {
        "start": ((880, 0.07), (0, 0.03), (1175, 0.09)),
        "recording": ((1046, 0.055),),
        "stop": ((1175, 0.07), (0, 0.03), (660, 0.11)),
        "error": ((220, 0.12), (0, 0.04), (220, 0.12)),
    }
    for frequency, duration in patterns.get(cue, ()):
        if frequency <= 0:
            time.sleep(duration)
        else:
            play_tone(args, frequency, duration)


def actual_backend(model: WhisperModel) -> str:
    inner = getattr(model, "model", None)
    device = getattr(inner, "device", "unknown")
    device_index = getattr(inner, "device_index", "")
    compute_type = getattr(inner, "compute_type", "unknown")
    if device_index not in ("", None):
        return f"{device} {device_index}, {compute_type}"
    return f"{device}, {compute_type}"


def load_model(args: argparse.Namespace, *, force_cpu: bool = False) -> ModelState:
    device = "cpu" if force_cpu else args.device
    compute_type = "int8" if force_cpu and args.compute_type == "auto" else args.compute_type
    print(f"Loading faster-whisper {args.model!r} on {device} ({compute_type})...", flush=True)
    model = WhisperModel(
        args.model,
        device=device,
        compute_type=compute_type,
        local_files_only=not args.allow_download,
    )
    state = ModelState(model=model, requested_device=device, requested_compute_type=compute_type)
    print(f"Ready: {actual_backend(model)}", flush=True)
    return state


def transcribe(model_state: ModelState, wav_path: Path, args: argparse.Namespace) -> tuple[str, ModelState]:
    kwargs = {"beam_size": args.beam_size}
    if args.language:
        kwargs["language"] = args.language

    try:
        segments, info = model_state.model.transcribe(str(wav_path), **kwargs)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text, model_state
    except Exception as exc:
        if model_state.requested_device == "cpu":
            raise
        print(f"GPU/auto transcription failed ({exc}); retrying on CPU int8...", flush=True)
        cpu_state = load_model(args, force_cpu=True)
        segments, info = cpu_state.model.transcribe(str(wav_path), **kwargs)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text, cpu_state


def run_osascript(script: str, *script_args: str, timeout: Optional[float] = None):
    """Run an AppleScript whose inputs arrive through `argv`.

    Values are passed as process arguments, so text with quotes or backslashes needs
    no AppleScript escaping.
    """
    return subprocess.run(
        ["osascript", "-e", script, *script_args],
        capture_output=True,
        text=True, encoding="utf-8",
        check=False,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def osascript_error(result) -> str:
    detail = (result.stderr or "").strip() or f"osascript exited {result.returncode}"
    if "-1743" in detail:
        return f"{detail}\n{MACOS_AUTOMATION_HINT}"
    if "-1719" in detail or "assistive access" in detail.lower():
        return f"{detail}\n{MACOS_ACCESSIBILITY_HINT}"
    return detail


def macos_accessibility_status() -> str:
    try:
        result = run_osascript(MACOS_ACCESSIBILITY_PROBE, timeout=10.0)
    except Exception as exc:
        return f"unknown ({exc})"
    if result.returncode == 0:
        return "granted" if result.stdout.strip() == "true" else "denied"
    detail = (result.stderr or "").strip()
    if "-1743" in detail:
        return "automation permission missing"
    return f"denied ({detail})"


def notify(title: str, message: str, args: argparse.Namespace) -> None:
    if not getattr(args, "notify", True):
        return
    if IS_MACOS:
        try:
            run_osascript(MACOS_NOTIFY_SCRIPT, title, message, timeout=10.0)
        except Exception:
            pass
        return
    notify_send = shutil.which("notify-send")
    if not notify_send or not os.getenv("DISPLAY"):
        return
    subprocess.run([notify_send, title, message], check=False, stdin=subprocess.DEVNULL)


def read_pid(path: Path = PID_FILE) -> Optional[int]:
    try:
        value = path.read_text(encoding="utf-8").strip()
        return int(value) if value else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def remove_pid(pid: Optional[int] = None, path: Path = PID_FILE) -> None:
    current = read_pid(path)
    if pid is not None and current not in (None, pid):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def write_pid(path: Path = PID_FILE) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def display_server() -> str:
    if IS_MACOS:
        return "macos"
    session_type = os.getenv("XDG_SESSION_TYPE", "").lower()
    if session_type in ("wayland", "x11"):
        return session_type
    if os.getenv("WAYLAND_DISPLAY"):
        return "wayland"
    if os.getenv("DISPLAY"):
        return "x11"
    return "unknown"


def macos_pasteboard_write(text: str) -> bool:
    """Put text on the pasteboard through AppKit rather than pbcopy.

    pbcopy means handing bytes to another process and trusting it to decode them with the
    encoding we meant. Inside an app bundle that negotiation goes wrong and non-ASCII text
    arrives as UTF-8 read through Mac OS Roman. Going through NSPasteboard passes a string
    across the bridge with no byte encoding anywhere in the path, so there is nothing to
    disagree about. Returns False when AppKit is unavailable, as in the plain CLI.
    """

    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString
    except ImportError:
        return False
    pasteboard = NSPasteboard.generalPasteboard()
    pasteboard.clearContents()
    return bool(pasteboard.setString_forType_(text, NSPasteboardTypeString))


def macos_pasteboard_read() -> Optional[str]:
    """Read the pasteboard through AppKit. None means AppKit is unavailable."""

    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString
    except ImportError:
        return None
    value = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
    return "" if value is None else str(value)


def copy_to_clipboard(text: str) -> bool:
    if IS_MACOS:
        if macos_pasteboard_write(text):
            return True
        pbcopy = shutil.which("pbcopy")
        if not pbcopy:
            return False
        subprocess.run([pbcopy], input=text, text=True, encoding="utf-8", check=True)
        return True

    if display_server() == "wayland":
        wl_copy = shutil.which("wl-copy")
        if not os.getenv("WAYLAND_DISPLAY") or not wl_copy:
            return False
        subprocess.run([wl_copy], input=text, text=True, encoding="utf-8", check=True)
        return True

    xclip = shutil.which("xclip")
    if not os.getenv("DISPLAY") or not xclip:
        return False
    subprocess.run(
        [xclip, "-selection", "clipboard"],
        input=text,
        text=True, encoding="utf-8",
        check=True,
    )
    return True


def macos_keystroke(text: str, *, command: bool = False, timeout: float = 60.0) -> bool:
    script = MACOS_COMMAND_KEY_SCRIPT if command else MACOS_KEYSTROKE_SCRIPT
    result = run_osascript(script, text, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(osascript_error(result))
    return True


def macos_paste(text: str) -> bool:
    if not copy_to_clipboard(text):
        return False
    time.sleep(0.15)
    return macos_keystroke("v", command=True, timeout=15.0)


def type_text(text: str, args: argparse.Namespace) -> bool:
    if IS_MACOS:
        # System Events types the whole string at its own pace, so --type-delay has
        # no effect here. Long transcripts are much faster through --output-method paste.
        return macos_keystroke(text)

    if display_server() == "wayland":
        ydotool = shutil.which("ydotool")
        if not ydotool:
            return False
        subprocess.run(
            [ydotool, "type", "--key-delay", str(args.type_delay), "--", text],
            check=True,
        )
        return True

    xdotool = shutil.which("xdotool")
    if not os.getenv("DISPLAY") or not xdotool:
        return False
    subprocess.run(
        [
            xdotool,
            "type",
            "--clearmodifiers",
            "--delay",
            str(args.type_delay),
            "--",
            text,
        ],
        check=True,
    )
    return True


def paste_clipboard(text: str, args: argparse.Namespace, shortcut: str = "ctrl+v") -> bool:
    if IS_MACOS:
        # Cmd+V pastes in every macOS app, Terminal and iTerm included, so the
        # "paste" and "terminal-paste" output methods collapse to one keystroke.
        return macos_paste(text)

    if display_server() == "wayland":
        ydotool = shutil.which("ydotool")
        if not ydotool or not copy_to_clipboard(text):
            return False
        time.sleep(0.15)
        subprocess.run([ydotool, "key", shortcut], check=True)
        return True

    xdotool = shutil.which("xdotool")
    if not os.getenv("DISPLAY") or not xdotool:
        return False
    if not copy_to_clipboard(text):
        return False
    time.sleep(0.15)
    subprocess.run([xdotool, "key", "--clearmodifiers", shortcut], check=True)
    return True


def insert_text(text: str, args: argparse.Namespace) -> bool:
    if not text:
        return False
    if not args.paste:
        return False

    try:
        if args.output_method == "type":
            return type_text(text, args)
        if args.output_method == "paste":
            return paste_clipboard(text, args, "ctrl+v")
        if args.output_method == "terminal-paste":
            return paste_clipboard(text, args, "ctrl+shift+v")
        if args.output_method == "clipboard":
            return copy_to_clipboard(text)
        raise ValueError(f"unknown output method: {args.output_method}")
    except Exception as exc:
        notify(APP_NAME, f"Could not insert transcript: {exc}", args)
        return False


def read_x_selection(selection: str) -> str:
    if IS_MACOS:
        # macOS has no PRIMARY selection; only the clipboard is readable.
        if selection != "clipboard":
            return ""
        native = macos_pasteboard_read()
        if native is not None:
            return native.strip()
        pbpaste = shutil.which("pbpaste")
        if not pbpaste:
            return ""
        try:
            result = subprocess.run(
                [pbpaste], capture_output=True, text=True, encoding="utf-8", check=False, timeout=2.0
            )
        except Exception:
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    if display_server() == "wayland":
        wl_paste = shutil.which("wl-paste")
        if not os.getenv("WAYLAND_DISPLAY") or not wl_paste:
            return ""
        command = [wl_paste, "--no-newline"]
        if selection == "primary":
            command.append("--primary")
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
        except Exception:
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    xclip = shutil.which("xclip")
    if not os.getenv("DISPLAY") or not xclip:
        return ""
    try:
        result = subprocess.run(
            [xclip, "-selection", selection, "-o"],
            capture_output=True,
            text=True, encoding="utf-8",
            check=False,
            timeout=1.0,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def macos_selected_text() -> tuple[str, str]:
    """Approximate the X11 PRIMARY selection by copying from the focused app.

    macOS exposes highlighted text only through a copy, so this sends Cmd+C and reads
    the clipboard. Any previous clipboard contents are put back afterwards, so a
    --speak-selection call leaves a non-empty clipboard as it found it.
    """
    before = read_x_selection("clipboard")
    try:
        macos_keystroke("c", command=True, timeout=15.0)
    except Exception:
        return "", ""
    time.sleep(0.2)
    after = read_x_selection("clipboard")
    if not after or after == before:
        return "", ""
    if before:
        try:
            copy_to_clipboard(before)
        except Exception:
            pass
    return after, "selection"


def selected_or_clipboard_text(copy_selection: bool = True) -> tuple[str, str]:
    if IS_MACOS:
        if copy_selection:
            text, source = macos_selected_text()
            if text:
                return text, source
        clipboard = read_x_selection("clipboard")
        if clipboard:
            return clipboard, "clipboard"
        return "", ""

    primary = read_x_selection("primary")
    if primary:
        return primary, "selection"

    clipboard = read_x_selection("clipboard")
    if clipboard:
        return clipboard, "clipboard"

    return "", ""


async def synthesize_edge_tts(text: str, output_path: Path, args: argparse.Namespace) -> None:
    import edge_tts

    kwargs = {"voice": args.tts_voice}
    rate = edge_rate_from_speed(args.tts_speed)
    if rate:
        kwargs["rate"] = rate

    communicate = edge_tts.Communicate(text, **kwargs)
    await communicate.save(str(output_path))


# espeak-ng aborts the whole process when it cannot read its data files, so the probe
# below runs in a child interpreter and only the exit status is trusted.
ESPEAK_PROBE_CODE = """
import ctypes
import sys

library, data_path = sys.argv[1], sys.argv[2] or None
espeak = ctypes.cdll.LoadLibrary(library)
# espeak_Initialize takes a const char *, so the data path has to be declared and
# encoded. Handing ctypes a str instead passes a wchar_t *, which the library reads
# as a one-character path, and the probe would then fail for the wrong reason.
espeak.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
espeak.espeak_Initialize.restype = ctypes.c_int
# AUDIO_OUTPUT_SYNCHRONOUS; returns the sample rate on success and -1 on failure.
sys.exit(0 if espeak.espeak_Initialize(0x02, 0, data_path.encode() if data_path else None, 0) > 0 else 1)
"""


def espeak_library_usable(library: str | os.PathLike[str], data_path: Optional[str] = None) -> bool:
    """Whether espeak-ng can be loaded *and* initialized from this library."""

    argv = [sys.executable, "-I", "-c", ESPEAK_PROBE_CODE, str(library), str(data_path or "")]
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def bundled_espeak_paths() -> Optional[tuple[str, str]]:
    """The espeak-ng library and data directory that kokoro-onnx carries, if present."""

    try:
        import espeakng_loader
    except ImportError:
        return None

    try:
        return str(espeakng_loader.get_library_path()), str(espeakng_loader.get_data_path())
    except Exception:
        return None


def ensure_macos_espeak_library() -> Optional[str]:
    """Make sure the kokoro-onnx phonemizer can find a working espeak-ng library on macOS.

    kokoro-onnx resolves espeak-ng through the espeakng-loader wheel and only consults
    PHONEMIZER_ESPEAK_LIBRARY as an override. On macOS the wheel's libespeak-ng.dylib
    loads but ignores the data directory it is handed and looks for its own build-time
    path instead, which kills the process. The bundled copy is therefore probed rather
    than trusted, and the Homebrew library is exported before kokoro_onnx is imported
    when the probe fails, so the dylib never has to be pointed at by hand.

    Returns the path that was exported, or None when nothing had to be changed.
    """

    if not IS_MACOS:
        return None

    if os.getenv("PHONEMIZER_ESPEAK_LIBRARY"):
        # An explicit override wins; a broken one is the caller's to fix.
        return None

    bundled = bundled_espeak_paths()
    if bundled and espeak_library_usable(*bundled):
        return None

    for lib_dir in MACOS_ESPEAK_LIB_DIRS:
        for lib_name in MACOS_ESPEAK_LIB_NAMES:
            candidate = Path(lib_dir) / lib_name
            # The data directory stays unset so the library falls back to its own.
            if candidate.exists() and espeak_library_usable(candidate):
                os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = str(candidate)
                return str(candidate)

    found = ctypes.util.find_library("espeak-ng") or ctypes.util.find_library("espeak")
    if found and espeak_library_usable(found):
        os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = found
        return found

    print(MACOS_ESPEAK_HINT, file=sys.stderr)
    return None


def synthesize_kokoro_tts(text: str, output_path: Path, args: argparse.Namespace) -> None:
    import soundfile as sf

    ensure_macos_espeak_library()

    from kokoro_onnx import Kokoro

    model_path = Path(args.kokoro_model).expanduser()
    voices_path = Path(args.kokoro_voices).expanduser()
    if not model_path.exists():
        raise RuntimeError(f"Kokoro model not found: {model_path}")
    if not voices_path.exists():
        raise RuntimeError(f"Kokoro voices file not found: {voices_path}")

    kokoro = Kokoro(str(model_path), str(voices_path))
    text = normalize_tts_text(text)
    audio, sample_rate = kokoro.create(
        text,
        voice=args.tts_voice,
        speed=args.tts_speed,
        lang=args.kokoro_lang,
        trim=args.kokoro_trim,
    )
    sf.write(output_path, audio, sample_rate)


def synthesize_tts(text: str, output_path: Path, args: argparse.Namespace) -> None:
    if args.tts_backend == "edge":
        asyncio.run(synthesize_edge_tts(text, output_path, args))
        return
    if args.tts_backend == "kokoro":
        synthesize_kokoro_tts(text, output_path, args)
        return
    raise ValueError(f"unknown TTS backend: {args.tts_backend}")


def tts_audio_suffix(args: argparse.Namespace) -> str:
    return ".mp3" if args.tts_backend == "edge" else ".wav"


def play_tts_audio(audio_path: Path) -> None:
    if IS_MACOS:
        # afplay ships with macOS and handles both the WAV and MP3 output, so ffmpeg
        # stays optional here.
        afplay = shutil.which("afplay")
        if afplay:
            subprocess.run([afplay, str(audio_path)], check=True, stdin=subprocess.DEVNULL)
            return

    ffplay = shutil.which("ffplay")
    if not ffplay:
        raise RuntimeError("ffplay is required to play TTS audio")
    subprocess.run(
        [ffplay, "-nodisp", "-autoexit", "-hide_banner", "-loglevel", "error", str(audio_path)],
        check=True,
        stdin=subprocess.DEVNULL,
    )


def stop_tts(args: argparse.Namespace, *, quiet: bool = False) -> bool:
    pid = read_pid(TTS_PID_FILE)
    if pid and process_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        remove_pid(pid, TTS_PID_FILE)
        if not quiet:
            print("Stopped voice TTS.")
            notify("Voice TTS", "Speech stopped.", args)
        return True

    if pid:
        remove_pid(pid, TTS_PID_FILE)
    if not quiet:
        print("Voice TTS is idle.")
    return False


def tts_background_argv(args: argparse.Namespace, text_file: Path) -> list[str]:
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--tts-background",
        "--tts-text-file",
        str(text_file),
        "--tts-backend",
        args.tts_backend,
        "--tts-voice",
        args.tts_voice,
        "--tts-speed",
        str(args.tts_speed),
        "--kokoro-model",
        str(args.kokoro_model),
        "--kokoro-voices",
        str(args.kokoro_voices),
        "--kokoro-lang",
        args.kokoro_lang,
    ]
    argv.append("--kokoro-trim" if args.kokoro_trim else "--kokoro-no-trim")
    if not args.notify:
        argv.append("--no-notify")
    return argv


def start_tts_background(text: str, args: argparse.Namespace, source: str = "text") -> int:
    text = text.strip()
    if not text:
        notify("Voice TTS", "No text to speak.", args)
        print("No text to speak.")
        return 1

    stop_tts(args, quiet=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = tempfile.NamedTemporaryFile(
        prefix="voice-tts-", suffix=".txt", dir=STATE_DIR, mode="w", encoding="utf-8", delete=False
    )
    text_file = Path(temp.name)
    with temp:
        temp.write(text)

    log = TTS_LOG_FILE.open("a", encoding="utf-8")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} speak {source} ---\n")
    log.flush()
    subprocess.Popen(
        tts_background_argv(args, text_file),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    log.close()
    print(f"Speaking {source} with {args.tts_backend}/{args.tts_voice} at {args.tts_speed:g}x.")
    return 0


def speak_selection(args: argparse.Namespace) -> int:
    pid = read_pid(TTS_PID_FILE)
    if pid and process_alive(pid):
        stop_tts(args)
        return 0
    if pid:
        remove_pid(pid, TTS_PID_FILE)

    text, source = selected_or_clipboard_text(getattr(args, "macos_copy_selection", True))
    if not text:
        notify("Voice TTS", "No selected text or clipboard text.", args)
        print("No selected text or clipboard text.")
        return 1
    return start_tts_background(text, args, source)


def run_tts_background(args: argparse.Namespace) -> int:
    if args.tts_text_file is None:
        print("voice TTS background error: missing --tts-text-file", file=sys.stderr, flush=True)
        return 1

    pid = os.getpid()
    text_file = args.tts_text_file
    audio_path: Optional[Path] = None
    should_delete_audio = False

    def request_stop(signum, frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGUSR1, request_stop)
    write_pid(TTS_PID_FILE)

    try:
        text = text_file.read_text(encoding="utf-8").strip()
        if not text:
            raise RuntimeError("no text to speak")

        temp = tempfile.NamedTemporaryFile(prefix="voice-tts-", suffix=tts_audio_suffix(args), delete=False)
        audio_path = Path(temp.name)
        temp.close()
        should_delete_audio = True

        notify("Voice TTS", "Speaking selected text...", args)
        print(
            f"Synthesizing {len(text)} chars with {args.tts_backend}/{args.tts_voice} at {args.tts_speed:g}x...",
            flush=True,
        )
        synthesize_tts(text, audio_path, args)
        print(f"Playing {audio_path}...", flush=True)
        play_tts_audio(audio_path)
        notify("Voice TTS", "Speech finished.", args)
        return 0
    except KeyboardInterrupt:
        print("Voice TTS stopped.", flush=True)
        return 0
    except Exception as exc:
        notify("Voice TTS", str(exc), args)
        print(f"voice TTS background error: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            text_file.unlink()
        except OSError:
            pass
        if should_delete_audio and audio_path:
            try:
                audio_path.unlink()
            except OSError:
                pass
        remove_pid(pid, TTS_PID_FILE)


def kokoro_voice_names(args: argparse.Namespace) -> list[str]:
    voices_path = Path(args.kokoro_voices).expanduser()
    if not voices_path.exists():
        raise RuntimeError(f"Kokoro voices file not found: {voices_path}")
    voices = np.load(voices_path)
    return list(sorted(voices.keys()))


def print_tts_voices(args: argparse.Namespace) -> int:
    try:
        voices = kokoro_voice_names(args)
    except Exception as exc:
        print(f"Could not list Kokoro voices: {exc}", file=sys.stderr)
        return 1

    print(f"{len(voices)} Kokoro voices")
    print(f"default: {DEFAULT_KOKORO_VOICE}")
    print(f"secondary: {SECONDARY_KOKORO_VOICE}")
    print("")
    for voice in voices:
        marker = ""
        if voice == DEFAULT_KOKORO_VOICE:
            marker = "  default"
        elif voice == SECONDARY_KOKORO_VOICE:
            marker = "  secondary"
        print(f"{voice}{marker}")
    return 0


def espeak_library_report() -> str:
    """One-line summary of which espeak-ng library the Kokoro backend will phonemize with."""

    override = os.getenv("PHONEMIZER_ESPEAK_LIBRARY")
    if override:
        state = "works" if espeak_library_usable(override) else "does not initialize"
        return f"{override} (PHONEMIZER_ESPEAK_LIBRARY, {state})"

    bundled = bundled_espeak_paths()
    if bundled and espeak_library_usable(*bundled):
        return f"{bundled[0]} (bundled with kokoro-onnx)"

    resolved = ensure_macos_espeak_library()
    if resolved:
        return f"{resolved} (system library, exported automatically)"

    return "missing"


def print_tts_check(args: argparse.Namespace) -> int:
    import importlib.metadata as importlib_metadata

    def version(package: str) -> str:
        try:
            return importlib_metadata.version(package)
        except Exception:
            return "not installed"

    print(f"backend: {args.tts_backend}")
    print(f"voice: {args.tts_voice}")
    print(f"speed: {args.tts_speed:g}x")
    if args.tts_backend == "edge":
        print(f"edge rate: {edge_rate_from_speed(args.tts_speed) or '+0%'}")
    if args.tts_backend == "kokoro":
        print(f"kokoro model: {Path(args.kokoro_model).expanduser()}")
        print(f"kokoro voices: {Path(args.kokoro_voices).expanduser()}")
        print(f"kokoro lang: {args.kokoro_lang}")
        print(f"kokoro trim: {args.kokoro_trim}")
        try:
            print(f"kokoro voice count: {len(kokoro_voice_names(args))}")
        except Exception as exc:
            print(f"kokoro voice count: unavailable ({exc})")
        print(f"secondary voice: {SECONDARY_KOKORO_VOICE}")
        print(f"espeak-ng library: {espeak_library_report()}")
    print(f"edge-tts: {version('edge-tts')}")
    print(f"kokoro-onnx: {version('kokoro-onnx')}")
    print(f"soundfile: {version('soundfile')}")
    print_platform_tools()
    return 0


def print_platform_tools() -> None:
    print(f"platform: {sys.platform}")
    print(f"display server: {display_server()}")
    if IS_MACOS:
        print(f"afplay: {shutil.which('afplay') or 'missing'}")
        print(f"ffplay: {shutil.which('ffplay') or 'optional on macOS (afplay is preferred)'}")
        print(f"pbcopy: {shutil.which('pbcopy') or 'missing'}")
        print(f"pbpaste: {shutil.which('pbpaste') or 'missing'}")
        print(f"osascript: {shutil.which('osascript') or 'missing'}")
        print(f"skhd: {shutil.which('skhd') or 'missing (needed for --install-hotkeys)'}")
        print(f"accessibility: {macos_accessibility_status()}")
        return
    print(f"ffplay: {shutil.which('ffplay') or 'missing'}")
    print(f"xclip: {shutil.which('xclip') or 'missing'}")
    print(f"wl-copy: {shutil.which('wl-copy') or 'missing'}")
    print(f"xdotool: {shutil.which('xdotool') or 'missing'}")
    print(f"ydotool: {shutil.which('ydotool') or 'missing'}")
    print(f"notify-send: {shutil.which('notify-send') or 'missing'}")


def background_argv(args: argparse.Namespace) -> list[str]:
    argv = [sys.executable, str(Path(__file__).resolve()), "--record-background"]
    argv += ["--model", args.model]
    argv += ["--device", args.device]
    argv += ["--compute-type", args.compute_type]
    argv += ["--beam-size", str(args.beam_size)]
    if args.language:
        argv += ["--language", args.language]
    if args.input_device:
        argv += ["--input-device", args.input_device]
    if args.sample_rate:
        argv += ["--sample-rate", str(args.sample_rate)]
    if args.save_dir:
        argv += ["--save-dir", str(args.save_dir)]
    if args.allow_download:
        argv.append("--allow-download")
    argv += ["--output-method", args.output_method]
    argv += ["--type-delay", str(args.type_delay)]
    argv += ["--recording-beep-interval", str(args.recording_beep_interval)]
    argv += ["--beep-volume", str(args.beep_volume)]
    if args.beep_output_device:
        argv += ["--beep-output-device", args.beep_output_device]
    argv.append("--beep" if args.beep else "--no-beep")
    if not args.paste:
        argv.append("--no-paste")
    if not args.notify:
        argv.append("--no-notify")
    return argv


def toggle_background_recording(args: argparse.Namespace) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    pid = read_pid()
    if pid and process_alive(pid):
        os.kill(pid, signal.SIGUSR1)
        print("Stopping voice recording...")
        return 0
    if pid:
        remove_pid(pid)

    log = LOG_FILE.open("a", encoding="utf-8")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} start ---\n")
    log.flush()
    subprocess.Popen(
        background_argv(args),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    log.close()
    print("Started voice recording. Run `voice --toggle` again to stop.")
    return 0


def run_background_recording(args: argparse.Namespace) -> int:
    input_device = parse_device(args.input_device)
    sample_rate = resolve_sample_rate(input_device, args.sample_rate)
    recorder = Recorder(sample_rate=sample_rate, input_device=input_device)
    stop_requested = False
    pid = os.getpid()

    def request_stop(signum, frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGUSR1, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    write_pid()

    wav_path: Optional[Path] = None
    should_delete = False
    try:
        play_cue(args, "start")
        recorder.start()
        notify(APP_NAME, "Recording...", args)
        print(f"Recording in background at {sample_rate} Hz. PID {pid}.", flush=True)
        if args.beep and args.recording_beep_interval > 0:
            print(f"Recording reminder cue every {args.recording_beep_interval:g}s.", flush=True)
        next_recording_cue = (
            time.monotonic() + args.recording_beep_interval
            if args.beep and args.recording_beep_interval > 0
            else None
        )

        while not stop_requested:
            if next_recording_cue is not None and time.monotonic() >= next_recording_cue:
                play_cue(args, "recording")
                next_recording_cue = time.monotonic() + args.recording_beep_interval
            time.sleep(0.05)

        wav_path, duration, should_delete = recorder.stop_to_wav(args.save_dir)
        play_cue(args, "stop")
        notify(APP_NAME, "Transcribing...", args)
        print(f"Stopped ({duration:.1f}s). Transcribing {wav_path}...", flush=True)

        model_state = load_model(args)
        text, _ = transcribe(model_state, wav_path, args)
        print(text or "[no speech detected]", flush=True)

        if text and args.paste:
            if insert_text(text, args):
                print("Transcript inserted.", flush=True)
                notify(APP_NAME, "Transcript inserted.", args)
            else:
                print("Transcript insert failed.", flush=True)
                notify(APP_NAME, "Transcript insert failed; see log.", args)
        elif text:
            notify(APP_NAME, "Transcript ready; see log.", args)
        else:
            notify(APP_NAME, "No speech detected.", args)
        return 0
    except Exception as exc:
        play_cue(args, "error")
        notify(APP_NAME, str(exc), args)
        print(f"voice background error: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if recorder.recording:
            try:
                recorder.stop_to_wav(None)
            except Exception:
                pass
        if should_delete and wav_path:
            try:
                wav_path.unlink()
            except OSError:
                pass
        remove_pid(pid)


def parse_gsettings_list(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("@as "):
        raw = raw[4:]
    try:
        parsed = ast.literal_eval(raw)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def set_gnome_custom_binding(schema: str, name: str, command: str, binding: str) -> None:
    subprocess.run(["gsettings", "set", schema, "name", name], check=True)
    subprocess.run(["gsettings", "set", schema, "command", command], check=True)
    subprocess.run(["gsettings", "set", schema, "binding", binding], check=True)


def install_gnome_hotkey() -> int:
    voice_cmd = str(Path.home() / ".local" / "bin" / "voice")
    stt_command = voice_cmd + " --toggle"
    tts_command = voice_cmd + " --speak-selection"
    media_schema = "org.gnome.settings-daemon.plugins.media-keys"

    result = subprocess.run(
        ["gsettings", "get", media_schema, "custom-keybindings"],
        capture_output=True,
        text=True, encoding="utf-8",
        check=True,
    )
    bindings = parse_gsettings_list(result.stdout)
    changed = False
    for path in (GNOME_BINDING_PATH, GNOME_TTS_BINDING_PATH):
        if path not in bindings:
            bindings.append(path)
            changed = True
    if changed:
        subprocess.run(
            ["gsettings", "set", media_schema, "custom-keybindings", repr(bindings)],
            check=True,
        )

    set_gnome_custom_binding(GNOME_BINDING_SCHEMA, APP_NAME, stt_command, "<Super>b")
    set_gnome_custom_binding(GNOME_TTS_BINDING_SCHEMA, "Voice TTS", tts_command, "<Super>t")
    print(f"Installed GNOME shortcut Super+B -> {stt_command}")
    print(f"Installed GNOME shortcut Super+T -> {tts_command}")
    return 0


def resolve_voice_command() -> str:
    """Return a shell-ready command that runs this voicemode installation.

    skhd hands its commands to a shell, so every path is quoted. The console script
    living beside the running interpreter is preferred over PATH, because a
    virtualenv that is not activated would otherwise resolve to a different install.
    """
    candidate = Path(sys.executable).parent / "voicemode"
    if candidate.exists():
        return shlex.quote(str(candidate))
    installed = shutil.which("voicemode") or shutil.which("voice")
    if installed:
        return shlex.quote(installed)
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"


def skhd_hotkey_block(voice_cmd: str) -> str:
    return "\n".join(
        [
            SKHD_BEGIN,
            f"{MACOS_STT_BINDING} : {voice_cmd} --toggle",
            f"{MACOS_TTS_BINDING} : {voice_cmd} --speak-selection",
            SKHD_END,
        ]
    )


def start_skhd_service(skhd: str) -> tuple[bool, str]:
    restart = subprocess.run([skhd, "--restart-service"], capture_output=True, text=True, encoding="utf-8", check=False)
    if restart.returncode == 0:
        return True, "restarted"
    install = subprocess.run([skhd, "--install-service"], capture_output=True, text=True, encoding="utf-8", check=False)
    start = subprocess.run([skhd, "--start-service"], capture_output=True, text=True, encoding="utf-8", check=False)
    if start.returncode == 0:
        return True, "installed and started"
    detail = (start.stderr or start.stdout or install.stderr or "").strip()
    return False, detail or f"skhd --start-service exited {start.returncode}"


def install_macos_hotkeys() -> int:
    skhd = shutil.which("skhd")
    if not skhd:
        print(
            "skhd not found. skhd lives in a third-party tap, so it takes three steps: "
            "brew tap koekeishiya/formulae && brew trust --formula koekeishiya/formulae/skhd "
            "&& brew install koekeishiya/formulae/skhd",
            file=sys.stderr,
        )
        return 1

    voice_cmd = resolve_voice_command()
    block = skhd_hotkey_block(voice_cmd)
    existing = SKHD_CONFIG.read_text(encoding="utf-8") if SKHD_CONFIG.exists() else ""
    marker = re.compile(rf"{re.escape(SKHD_BEGIN)}.*?{re.escape(SKHD_END)}\n?", re.DOTALL)
    if marker.search(existing):
        updated = marker.sub(block + "\n", existing)
        action = "Updated"
    else:
        prefix = existing if (not existing or existing.endswith("\n")) else existing + "\n"
        updated = f"{prefix}{block}\n"
        action = "Added"
    SKHD_CONFIG.write_text(updated, encoding="utf-8")

    print(f"{action} voicemode bindings in {SKHD_CONFIG}")
    print(f"Installed shortcut {MACOS_STT_LABEL} -> {voice_cmd} --toggle")
    print(f"Installed shortcut {MACOS_TTS_LABEL} -> {voice_cmd} --speak-selection")

    ok, detail = start_skhd_service(skhd)
    if ok:
        print(f"skhd service {detail}.")
    else:
        print(f"skhd service did not start: {detail}", file=sys.stderr)
        print(f"Start it by hand with: {skhd} --start-service", file=sys.stderr)

    print("")
    print("macOS still needs two permissions before the hotkeys work:")
    print("  1. System Settings > Privacy & Security > Accessibility: enable skhd")
    print("     (required so voicemode can type or paste the transcript)")
    print("  2. System Settings > Privacy & Security > Microphone: enable skhd")
    print("     (required so the background recorder can reach the microphone)")
    print("Run `voicemode --check` afterwards to confirm the Accessibility grant.")
    return 0 if ok else 1


def install_hotkeys() -> int:
    if IS_MACOS:
        return install_macos_hotkeys()
    return install_gnome_hotkey()


def print_status() -> int:
    pid = read_pid()
    if pid and process_alive(pid):
        print(f"recording pid {pid}")
        return 0
    if pid:
        remove_pid(pid)
    print("idle")
    return 0


def watch_log(args: argparse.Namespace) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    watched_logs = ((LOG_FILE, "STT"), (TTS_LOG_FILE, "TTS"))
    offsets = {path: path.stat().st_size if path.exists() else 0 for path, _ in watched_logs}

    print("Voice monitor running.")
    print("Press Super+B anywhere to start recording; press Super+B again to stop and transcribe.")
    print("Press Super+T to speak selected text; press Super+T again to stop speaking.")
    print("New transcript activity will appear here. Ctrl+C exits the monitor.")
    pid = read_pid()
    if pid and process_alive(pid):
        print(f"Recording is already active: pid {pid}")
    tts_pid = read_pid(TTS_PID_FILE)
    if tts_pid and process_alive(tts_pid):
        print(f"TTS is already active: pid {tts_pid}")
    print("")
    sys.stdout.flush()

    try:
        while True:
            for log_path, label in watched_logs:
                if not log_path.exists():
                    continue
                size = log_path.stat().st_size
                if size < offsets[log_path]:
                    offsets[log_path] = 0
                if size > offsets[log_path]:
                    with log_path.open("r", encoding="utf-8", errors="replace") as log:
                        log.seek(offsets[log_path])
                        data = log.read()
                        offsets[log_path] = log.tell()
                    if data:
                        if label == "TTS":
                            sys.stdout.write("[TTS]\n")
                        sys.stdout.write(data)
                        sys.stdout.flush()
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nVoice monitor stopped.")
        return 0


def run_terminal_mode(args: argparse.Namespace) -> int:
    input_device = parse_device(args.input_device)
    sample_rate = resolve_sample_rate(input_device, args.sample_rate)
    args.language = args.language.strip() or None

    model_state = load_model(args)
    if args.check:
        return 0

    recorder = Recorder(sample_rate=sample_rate, input_device=input_device)
    print("")
    print("Terminal mode: press Ctrl+B here to start recording, then Ctrl+B again to stop.")
    print(f"Audio: {sample_rate} Hz mono")

    try:
        next_recording_cue = None
        with TerminalKeys() as keys:
            while True:
                key = keys.read_key()
                if next_recording_cue is not None and time.monotonic() >= next_recording_cue:
                    play_cue(args, "recording")
                    next_recording_cue = time.monotonic() + args.recording_beep_interval
                if key != CTRL_B:
                    continue

                if not recorder.recording:
                    play_cue(args, "start")
                    recorder.start()
                    print("Recording...", flush=True)
                    if args.beep and args.recording_beep_interval > 0:
                        print(f"Recording reminder cue every {args.recording_beep_interval:g}s.", flush=True)
                    next_recording_cue = (
                        time.monotonic() + args.recording_beep_interval
                        if args.beep and args.recording_beep_interval > 0
                        else None
                    )
                    continue

                wav_path, duration, should_delete = recorder.stop_to_wav(args.save_dir)
                next_recording_cue = None
                play_cue(args, "stop")
                print(f"Stopped ({duration:.1f}s). Transcribing...", flush=True)
                try:
                    text, model_state = transcribe(model_state, wav_path, args)
                    print("")
                    print(text or "[no speech detected]")
                    print("")
                finally:
                    if should_delete:
                        try:
                            wav_path.unlink()
                        except OSError:
                            pass
                print("Press Ctrl+B to record again. Ctrl+C exits.")
    except KeyboardInterrupt:
        print("\nExiting.")
        return 0


def parse_args() -> argparse.Namespace:
    tts_defaults = load_hermes_tts_defaults()
    parser = argparse.ArgumentParser(description="Desktop-global STT and TTS voice command.")
    parser.add_argument("--model", default=os.getenv("VOICE_STT_MODEL", "base"))
    parser.add_argument("--device", default=os.getenv("VOICE_STT_DEVICE", "auto"))
    parser.add_argument("--compute-type", default=os.getenv("VOICE_STT_COMPUTE_TYPE", "auto"))
    parser.add_argument("--language", default=os.getenv("VOICE_STT_LANGUAGE", ""))
    parser.add_argument("--beam-size", type=int, default=int(os.getenv("VOICE_STT_BEAM_SIZE", "5")))
    parser.add_argument("--input-device", default=os.getenv("VOICE_INPUT_DEVICE"))
    parser.add_argument("--sample-rate", type=int, default=None)
    parser.add_argument("--save-dir", type=Path, default=None, help="Keep recorded wav files in this directory.")
    parser.add_argument("--allow-download", action="store_true", help="Allow faster-whisper to download a missing model.")
    parser.add_argument("--list-devices", action="store_true", help="Print sounddevice devices and exit.")
    parser.add_argument("--check", action="store_true", help="Load the model and print the selected backend, then exit.")
    parser.add_argument("--toggle", action="store_true", help="Toggle background recording; stop transcribes and inserts text.")
    parser.add_argument("--watch", action="store_true", help="Watch global hotkey activity in this terminal.")
    parser.add_argument("--terminal", action="store_true", help="Use terminal-only Ctrl+B recording mode.")
    parser.add_argument("--status", action="store_true", help="Print whether background recording is active.")
    parser.add_argument(
        "--install-hotkey",
        action="store_true",
        help="Install global shortcuts: GNOME keybindings on Linux, skhd bindings on macOS.",
    )
    parser.add_argument(
        "--install-hotkeys",
        action="store_true",
        help="Install global shortcuts: GNOME keybindings on Linux, skhd bindings on macOS.",
    )
    parser.add_argument("--record-background", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--speak-selection", action="store_true", help="Speak selected text, falling back to clipboard text.")
    parser.add_argument("--speak", metavar="TEXT", help="Speak the provided text using TTS.")
    parser.add_argument("--stop-tts", action="store_true", help="Stop active TTS playback.")
    parser.add_argument("--tts-check", action="store_true", help="Print TTS voice, speed, and dependency status.")
    parser.add_argument("--list-tts-voices", action="store_true", help="List available Kokoro voice IDs.")
    parser.add_argument(
        "--tts-backend",
        choices=("kokoro", "edge"),
        default=os.getenv("VOICE_TTS_BACKEND", DEFAULT_TTS_BACKEND),
        help="TTS backend. Default: kokoro.",
    )
    parser.add_argument(
        "--tts-voice",
        default=os.getenv("VOICE_TTS_VOICE"),
        help=f"TTS voice ID. Kokoro default: {DEFAULT_KOKORO_VOICE}; secondary: {SECONDARY_KOKORO_VOICE}.",
    )
    parser.add_argument(
        "--tts-speed",
        type=float,
        default=float(os.getenv("VOICE_TTS_SPEED", str(tts_defaults.speed))),
        help="TTS speed multiplier. Default: 2.0.",
    )
    parser.add_argument("--tts-background", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--tts-text-file", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--kokoro-model",
        type=Path,
        default=Path(os.getenv("VOICE_KOKORO_MODEL", str(default_kokoro_model_path()))),
        help="Kokoro ONNX model path.",
    )
    parser.add_argument(
        "--kokoro-voices",
        type=Path,
        default=Path(os.getenv("VOICE_KOKORO_VOICES", str(default_kokoro_voices_path()))),
        help="Kokoro voices file path.",
    )
    parser.add_argument("--kokoro-lang", default=os.getenv("VOICE_KOKORO_LANG", DEFAULT_KOKORO_LANG), help="Kokoro language code.")
    parser.add_argument(
        "--kokoro-trim",
        dest="kokoro_trim",
        action="store_true",
        default=env_bool("VOICE_KOKORO_TRIM", False),
        help="Trim leading/trailing silence in Kokoro chunks.",
    )
    parser.add_argument(
        "--kokoro-no-trim",
        dest="kokoro_trim",
        action="store_false",
        help="Keep Kokoro chunk boundaries untrimmed. Default.",
    )
    parser.add_argument(
        "--output-method",
        choices=("type", "paste", "terminal-paste", "clipboard"),
        default=os.getenv("VOICE_OUTPUT_METHOD", "paste" if IS_MACOS else "type"),
        help="How background mode inserts the transcript. Default: paste on macOS, type on Linux.",
    )
    parser.add_argument(
        "--type-delay",
        type=int,
        default=int(os.getenv("VOICE_TYPE_DELAY", "2")),
        help="Milliseconds between synthetic keystrokes for --output-method type. Ignored on macOS.",
    )
    parser.add_argument("--paste", dest="paste", action="store_true", default=True, help="Insert transcript into the focused app.")
    parser.add_argument("--no-paste", dest="paste", action="store_false", help="Do not insert after background transcription.")
    parser.add_argument(
        "--macos-copy-selection",
        dest="macos_copy_selection",
        action="store_true",
        default=os.getenv("VOICE_MACOS_COPY_SELECTION", "true").strip().lower() not in ("0", "false", "no"),
        help="On macOS, send Cmd+C before --speak-selection to capture highlighted text. Default.",
    )
    parser.add_argument(
        "--no-macos-copy-selection",
        dest="macos_copy_selection",
        action="store_false",
        help="On macOS, read only the existing clipboard for --speak-selection.",
    )
    parser.add_argument("--notify", dest="notify", action="store_true", default=True, help="Show desktop notifications.")
    parser.add_argument("--no-notify", dest="notify", action="store_false", help="Disable desktop notifications.")
    parser.add_argument("--beep", dest="beep", action="store_true", default=env_bool("VOICE_BEEP", True), help="Play audible recording cues.")
    parser.add_argument("--no-beep", dest="beep", action="store_false", help="Disable audible recording cues.")
    parser.add_argument(
        "--recording-beep-interval",
        type=float,
        default=float(os.getenv("VOICE_RECORDING_BEEP_INTERVAL", "5")),
        help="Seconds between reminder cues while recording; 0 disables reminders.",
    )
    parser.add_argument(
        "--beep-volume",
        type=float,
        default=float(os.getenv("VOICE_BEEP_VOLUME", "0.08")),
        help="Cue volume from 0.0 to 1.0.",
    )
    parser.add_argument("--beep-output-device", default=os.getenv("VOICE_BEEP_OUTPUT_DEVICE"), help="Output device for cues.")
    parser.add_argument("--test-beep", action="store_true", help="Play start, reminder, and stop cues, then exit.")
    args = parser.parse_args()
    if not args.tts_voice:
        args.tts_voice = DEFAULT_KOKORO_VOICE if args.tts_backend == "kokoro" else tts_defaults.voice
    return args


def main() -> int:
    args = parse_args()

    if args.list_tts_voices:
        return print_tts_voices(args)
    if args.tts_check:
        return print_tts_check(args)
    if args.tts_background:
        return run_tts_background(args)
    if args.stop_tts:
        return 0 if stop_tts(args) else 1
    if args.speak_selection:
        return speak_selection(args)
    if args.speak is not None:
        return start_tts_background(args.speak, args)
    if args.test_beep:
        play_cue(args, "start")
        time.sleep(0.4)
        play_cue(args, "recording")
        time.sleep(0.4)
        play_cue(args, "stop")
        return 0
    if args.list_devices:
        print(sd.query_devices())
        return 0
    if args.install_hotkey or args.install_hotkeys:
        return install_hotkeys()
    if args.status:
        return print_status()
    if args.toggle:
        return toggle_background_recording(args)

    input_device = parse_device(args.input_device)
    sample_rate = resolve_sample_rate(input_device, args.sample_rate)
    args.language = args.language.strip() or None

    if args.record_background:
        return run_background_recording(args)

    if args.check:
        load_model(args)
        print_platform_tools()
        return 0
    if args.terminal:
        return run_terminal_mode(args)
    return watch_log(args)


if __name__ == "__main__":
    raise SystemExit(main())
