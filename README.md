# voicemode

Desktop-global speech-to-text and text-to-speech hotkeys for Linux and macOS.

voicemode is a single Python command that runs local push-to-talk transcription and selected-text speech from a global shortcut. It was built for an X11 desktop where dictation should work in any focused application, including terminals, and it now carries a macOS backend that uses the same command surface.

## Current Status

This project is usable on the development workstation, but it is not a polished cross-desktop package yet.

Tested target:

```text
OS: Ubuntu/Linux
Desktop: GNOME on Wayland or X11
Python: 3.11
STT: faster-whisper
TTS default: Kokoro via kokoro-onnx
Shortcut manager: GNOME gsettings custom keybindings
Text insertion: ydotool on Wayland, xdotool on X11
Selection reading: wl-clipboard on Wayland, xclip on X11
```

macOS target:

```text
OS: macOS on Apple Silicon or Intel
Python: 3.11
STT: faster-whisper (CPU only; ctranslate2 has no Metal backend)
TTS default: Kokoro via kokoro-onnx
TTS playback: afplay, built in
Shortcut manager: skhd
Text insertion: osascript keystroke via System Events
Selection reading: Cmd+C into the clipboard, then pbpaste
```

Known non-goals for the current version:

```text
Non-GNOME shortcut installation on Linux
Shortcut managers other than skhd on macOS
Bundled model weights
Strict permissive-only TTS dependency chain
GPU-accelerated STT on macOS
```

## Features

- `Super+B` (Linux) / `Cmd+Option+B`, that is ⌘⌥B (macOS): toggle recording, then transcribe and insert text into the focused application.
- `Super+T` (Linux) / `Cmd+Option+T`, that is ⌘⌥T (macOS): speak selected text, with clipboard fallback.
- Same shortcut while speaking: stop current TTS playback.
- Local STT through `faster-whisper`.
- Local Kokoro TTS by default.
- Optional Microsoft Edge TTS backend for comparison.
- Audible recording cues with no external sound assets.
- Terminal monitor mode for background activity.

## Runtime Model

The installed GNOME shortcuts call the same command:

```text
Super+B -> voicemode --toggle
Super+T -> voicemode --speak-selection
```

For STT, `--toggle` starts a detached background recorder. The next toggle sends it a signal to stop recording, transcribe, and insert the recognized text.

For TTS, `--speak-selection` reads the X11 primary selection with `xclip -selection primary -o`. This is the text most X11 applications expose immediately after a mouse highlight. If the primary selection is empty, voicemode reads the clipboard instead. It then synthesizes a temporary audio file and plays it with `ffplay`.

macOS has no PRIMARY selection, so `--speak-selection` sends `Cmd+C` to the focused application, reads the clipboard with `pbpaste`, and then restores the previous clipboard contents. Pass `--no-macos-copy-selection` (or set `VOICE_MACOS_COPY_SELECTION=false`) to skip the copy and read only the existing clipboard. Playback uses `afplay`, so ffmpeg is optional on macOS.

Runtime state is stored under:

```text
$XDG_RUNTIME_DIR/voice-stt/
```

## Installation on Linux

Install system packages:

```bash
sudo apt install ffmpeg xclip xdotool ydotool wl-clipboard libportaudio2 portaudio19-dev libnotify-bin wget
```

Install the Python package in editable mode:

```bash
python3.11 -m pip install --user -e ".[kokoro]"
```

Download Kokoro model assets:

```bash
scripts/download-kokoro-assets.sh
```

Install GNOME hotkeys:

```bash
voicemode --install-hotkeys
```

## Installation on macOS

There are two ways to run this on macOS. The **menu-bar application** is the easier one: it
bundles its own Python, registers its own hotkeys, and needs no Homebrew packages, because it
does not depend on skhd at all. See [docs/MACOS-APP.md](docs/MACOS-APP.md) to build it.

The rest of this section covers the **command-line** setup, which runs the same code with
skhd providing the shortcuts. Do not run both: they would answer the same keystroke.


Nothing has to be installed for speech-to-text beyond the Python package: `sounddevice`
carries its own PortAudio, and playback uses `afplay`, which ships with macOS. Kokoro TTS
needs an espeak-ng library, but `kokoro-onnx` usually bundles a working one, so
`brew install espeak-ng` is a fallback rather than a prerequisite. See
[The espeak-ng library on macOS](#the-espeak-ng-library-on-macos) below.

`skhd` provides the global shortcuts. It is not in homebrew-core, so it takes a tap, a trust step for the tap's formula code, and then the install:

```bash
brew tap koekeishiya/formulae
brew trust --formula koekeishiya/formulae/skhd
brew install koekeishiya/formulae/skhd
```

`brew trust` is required because current Homebrew refuses to load a formula from an untrusted third-party tap. Two things to know before granting it: the formula's source URL now points at `github.com/asmvik/skhd` rather than `github.com/koekeishiya/skhd`, which is a GitHub account rename by the same author rather than a handover, and upstream skhd is in maintenance mode and takes critical fixes only. voicemode only relies on skhd reading `~/.skhdrc` and exec'ing a command, so maintenance mode is a caveat rather than a blocker. Skip all three commands if you only want the CLI and no global hotkeys.

The asset downloader uses `curl`, which ships with macOS, so nothing extra is needed for it.

Create a Python 3.11 environment and install the package. Use 3.11 rather than the system `python3`, because that is the version `ctranslate2` wheels are published for:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e ".[kokoro]"
```

Download Kokoro model assets:

```bash
scripts/download-kokoro-assets.sh
```

Install the skhd hotkeys:

```bash
.venv/bin/voicemode --install-hotkeys
```

### The espeak-ng library on macOS

No manual `export` is needed, and in most cases no `brew install` either. voicemode locates
a working espeak-ng library itself, and `--tts-check` reports which one it settled on:

```text
espeak-ng library: .../espeakng_loader/libespeak-ng.dylib (bundled with kokoro-onnx)
```

`kokoro-onnx` resolves espeak-ng through the `espeakng-loader` wheel, which bundles its own
`libespeak-ng.dylib`. That bundled library usually works on macOS and needs no system package.

Sometimes it does not, and the same wheel version has been seen both ways, so the install
rather than the version number decides. In the failing case the dylib loads, then ignores the
data directory it is handed and looks for its own build-time path instead, which kills the
process outright rather than raising:

```text
Error processing file '/Users/runner/work/espeakng-loader/.../espeak-ng-data/phontab': No such file or directory.
```

Because that failure cannot be caught in-process, voicemode probes the bundled library in a
child interpreter before trusting it. When the probe fails it falls back to the Homebrew
library (`/opt/homebrew/lib` on Apple Silicon, `/usr/local/lib` on Intel) and exports
`PHONEMIZER_ESPEAK_LIBRARY` before `kokoro_onnx` is imported. Setting that variable yourself
overrides the search entirely.

So install `espeak-ng` only if `--tts-check` tells you to:

```bash
brew install espeak-ng
```

### macOS permissions

The hotkeys stay silent until macOS grants two permissions to **skhd**, which is the process
that launches voicemode. Permissions are inherited from whatever starts the recorder, so
granting them to your terminal does nothing once skhd owns the shortcut.

**Accessibility**, which lets voicemode paste or type the transcript. skhd runs as a launchd
agent, so macOS never shows it a permission prompt and it will not appear in the list on its
own. Add the binary by hand:

```bash
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
```

Click **+**, press `Cmd+Shift+G`, enter `/opt/homebrew/bin/skhd`, add it, make sure its toggle
is on, then restart the service:

```bash
skhd --restart-service
```

To check that the grant took, see whether skhd is still running. It aborts within
milliseconds when it is not trusted, so a live process is the grant:

```bash
pgrep -lf /opt/homebrew/bin/skhd
```

Note that `/tmp/skhd_$USER.err.log` is append-only, so a `must be run with accessibility
access! abort..` line in it may be left over from an earlier attempt. Compare its timestamp
against the current process start time before believing it.

**Microphone**, which lets the background recorder open the input device. skhd does not appear
in this list until something beneath it first tries to record, so grant Accessibility first,
then press the dictation hotkey once and allow the prompt when it appears.

`--check` also prints an `accessibility:` line, but it reflects whichever process runs it, not
skhd. Run from a terminal it commonly reports a missing grant while the hotkeys work perfectly
well. Use it to check the espeak-ng and tool paths, not the skhd grant.

### Using the hotkeys

`alt` is what skhd calls the Option key, so the installed shortcuts are **Cmd+Option+B**
(⌘⌥B) and **Cmd+Option+T** (⌘⌥T).

Dictation is a toggle, not push-to-hold: press ⌘⌥B, speak, press ⌘⌥B again. A beep confirms
each press, and a reminder cue repeats every 5 seconds while recording. The first transcription
after a reboot takes roughly 20 seconds longer while the model loads.

Two things worth setting, both of which the hotkeys pick up from your shell environment:

```bash
export VOICE_STT_MODEL=small
export VOICE_STT_LANGUAGE=en
```

`small` punctuates and segments noticeably better than the default `base` for little extra
latency. Pinning the language skips autodetection, which is faster and stops Whisper
hallucinating text in another language when it is handed near-silence — a toggle started and
stopped without speaking will otherwise produce confident nonsense.

`VOICE_STT_LANGUAGE` takes any of Whisper's 100 language codes, such as `vi` for Vietnamese or
`zh` for Chinese; leave it empty to detect the language per recording. Note that this sets the
transcription language only. Kokoro TTS speaks English, so `--speak-selection` does not follow
it.

To try any of this without granting permissions at all, use terminal mode, which prints the
transcript locally instead of inserting it into another application:

```bash
.venv/bin/voicemode --terminal
```

The package also installs a `voice` command alias for compatibility with earlier local setup:

```bash
voice --install-hotkeys
```

## Usage

Start the monitor:

```bash
voicemode
```

Check STT backend loading:

```bash
voicemode --check
```

Check TTS configuration:

```bash
voicemode --tts-check
```

List Kokoro voices:

```bash
voicemode --list-tts-voices
```

Speak explicit text:

```bash
voicemode --speak "This is a local TTS test."
```

Speak selected text:

```bash
voicemode --speak-selection
```

Stop active TTS:

```bash
voicemode --stop-tts
```

## Default TTS Configuration

The current default TTS backend is Kokoro:

```text
backend: kokoro
voice: af_heart
speed: 2x
trim: false
secondary voice: bm_george
```

Use George manually:

```bash
voicemode --speak-selection --tts-voice bm_george
```

Use Edge TTS manually:

```bash
voicemode --tts-backend edge --tts-voice en-GB-RyanNeural --speak-selection
```

## Configuration

Common environment variables:

```text
VOICE_STT_MODEL=base
VOICE_STT_DEVICE=auto
VOICE_STT_COMPUTE_TYPE=auto
VOICE_STT_LANGUAGE=
VOICE_OUTPUT_METHOD=type
VOICE_TYPE_DELAY=2
VOICE_BEEP=true
VOICE_RECORDING_BEEP_INTERVAL=5
VOICE_TTS_BACKEND=kokoro
VOICE_TTS_VOICE=af_heart
VOICE_TTS_SPEED=2.0
VOICE_KOKORO_MODEL=/path/to/kokoro-v1.0.onnx
VOICE_KOKORO_VOICES=/path/to/voices-v1.0.bin
VOICE_KOKORO_TRIM=false
VOICE_MACOS_COPY_SELECTION=true
```

On macOS the default `VOICE_OUTPUT_METHOD` is `paste` rather than `type`, because `System Events` types character by character at its own pace and ignores `VOICE_TYPE_DELAY`. `paste` and `terminal-paste` both send `Cmd+V`, which works in Terminal and iTerm as well as in regular applications.

## Privacy

STT is local when using the default faster-whisper path. On macOS it runs on the CPU, since `ctranslate2` has no Metal or Neural Engine backend; the `base` and `small` models keep up with dictation, while `large-v3` is slow enough that a Metal-backed engine such as `mlx-whisper` or `whisper.cpp` is the better choice.

Kokoro TTS is local once model assets are downloaded.

Edge TTS is optional and sends text to Microsoft's online service through the `edge-tts` package. Do not use the Edge backend for private text unless that is acceptable.

## Repository Layout

```text
voice.py                         main command implementation
pyproject.toml                   package metadata and console scripts
scripts/download-kokoro-assets.sh local model asset downloader
docs/DEPENDENCIES.md             dependency and license notes
models/                          ignored local model files
samples/                         ignored generated audio samples
```

## License

The repository source code is MIT licensed.

Third-party packages, model weights, and system tools keep their own licenses. See `docs/DEPENDENCIES.md`.

The current Kokoro test path uses `kokoro-onnx==0.5.0`, which pulls a GPLv3+ phonemizer dependency. That is acceptable for local evaluation, but it is not a clean permissive-only dependency chain. Before publishing binary packages or claiming a strict MIT-style dependency stack, replace or isolate that dependency path.

The optional Edge backend is not an open local voice stack. The Python package is open source, but the service and voices are controlled by Microsoft.
