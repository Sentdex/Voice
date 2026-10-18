# Dependency and License Notes

This file tracks the non-repository components used by voicemode.

The repository source code is MIT licensed. Third-party packages, model weights, and system tools keep their own licenses.

## Python Packages

| Component | Use | License status |
| --- | --- | --- |
| faster-whisper | Local STT inference | MIT |
| ctranslate2 | Whisper inference runtime | MIT |
| numpy | Audio arrays | BSD-style permissive |
| sounddevice | Microphone and cue audio | MIT |
| PyYAML | Optional config parsing | MIT |
| kokoro-onnx | Local Kokoro TTS runtime | MIT package metadata, but current package dependency chain needs review |
| espeakng-loader | Pulled in by kokoro-onnx to locate espeak-ng | MIT; bundled macOS dylib is usually but not always usable, see below |
| phonemizer-fork | Pulled in by kokoro-onnx for phonemization | GPLv3+, see the caveat below |
| soundfile | WAV output for Kokoro TTS | BSD-style permissive |
| edge-tts | Optional Edge TTS backend | LGPLv3 package, service terms are separate |

## TTS Backend Caveats

Kokoro is the default local TTS backend in this working tree. The model weights are distributed separately and are not committed to the repository.

The currently tested `kokoro-onnx==0.5.0` package pulls `phonemizer-fork`, which reports GPLv3+ metadata. That is acceptable for local testing, but it is not a clean permissive-only dependency chain. Before publishing binary packages or claiming a strict MIT-style stack, replace or isolate that dependency path.

`kokoro-onnx` also pulls `espeakng-loader`, which bundles the espeak-ng shared library so that no system install is needed, and reads `PHONEMIZER_ESPEAK_LIBRARY` only as an override. On macOS that bundled `libespeak-ng.dylib` is unreliable: in some installs it works and needs nothing further, while in others it loads but ignores the data directory passed to `espeak_Initialize` and looks for its build-time path under `/Users/runner/work/...` instead, at which point espeak-ng prints a missing-`phontab` error and exits the process. Version 0.2.4 has been observed behaving both ways on the same machine, so the install decides rather than the version number, and a system `espeak-ng` is a fallback worth having rather than a standing requirement.

Because that failure kills the process instead of raising, voicemode probes the bundled library in a child interpreter and only exports `PHONEMIZER_ESPEAK_LIBRARY` to the Homebrew dylib when the probe fails. `--tts-check` reports which library was chosen. The Linux path keeps using the bundled copy untouched.

The Edge backend is optional. It uses the open-source `edge-tts` package, but sends text to Microsoft's online Edge TTS service. It should not be treated as an open local TTS engine.

## Platform Tools

| Component | Platform | Use |
| --- | --- | --- |
| xclip, xdotool, notify-send | Linux (X11) | Clipboard, typing, notifications |
| wl-clipboard, ydotool | Linux (Wayland) | Clipboard, typing |
| gsettings | Linux (GNOME) | Global shortcut installation |
| ffplay (ffmpeg) | Linux | TTS playback |
| pbcopy, pbpaste, osascript, afplay | macOS | Clipboard, keystrokes, notifications, playback; all ship with the OS |
| skhd | macOS | Global shortcut installation, MIT; third-party tap, needs `brew trust` |
| espeak-ng | macOS | Fallback for the Kokoro phonemizer when the bundled dylib fails; `brew install espeak-ng`, located automatically |

On macOS, `ctranslate2` runs faster-whisper on the CPU only; there is no Metal or Neural Engine backend. macOS also requires Accessibility and Microphone permissions for the process that launches voicemode, which is `skhd` once the hotkeys are installed.

## Local Assets

The following paths are intentionally ignored by git:

```text
models/
samples/
```

Use this helper to restore Kokoro assets after cloning:

```bash
scripts/download-kokoro-assets.sh
```
