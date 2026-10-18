# The Voicemode menu-bar application

A drag-to-Applications build of voicemode for macOS. It does the same thing the CLI does, but
it carries its own Python, registers its own hotkeys, and needs no Homebrew packages at all.

## Why the app needs less setup than the CLI

The CLI relies on `skhd` for global shortcuts. That costs a third-party tap, a `brew trust`,
and — because `skhd` runs as a launchd agent that macOS never prompts — manually adding its
binary to the Accessibility list.

The application registers its hotkeys itself, through Carbon's `RegisterEventHotKey`. That
API needs **no permission at all**, so there is nothing to grant before a keypress reaches the
app. The two permissions macOS genuinely requires are still required, but they are prompted
for normally, because this is a real bundle with a stable identity rather than a daemon:

| Permission | Asked for when | Needed for |
| --- | --- | --- |
| Microphone | first dictation | opening the input device |
| Accessibility | first transcript insertion | pasting into the focused app |

If Accessibility is refused, nothing is lost: the transcript goes to the clipboard instead and
the menu bar says so.

## Building it

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e ".[app]"
scripts/build-macos-app.sh
```

The result is `dist/Voicemode.app`. Drag it to `/Applications`.

### Signing is not optional in practice

The build script signs with the first **Apple Development** identity in your keychain, and
takes an identity name as its first argument to override that. Pass `NONE` to skip signing.

Skipping it is painful rather than merely untidy. macOS ties the Microphone and Accessibility
grants to the code signature, so an unsigned or ad-hoc-signed bundle is a *different*
application to TCC after every rebuild, and you re-grant both permissions each time.

`resources/Voicemode.entitlements` carries three entitlements: audio input, Apple Events, and
`disable-library-validation`. The last one matters because py2app bundles third-party `.so`
files that your team did not sign, and the hardened runtime would otherwise refuse to load
them.

### Giving it to someone else

An **Apple Development** certificate is for your own machines. Distributing the app to other
people needs a **Developer ID Application** certificate plus notarization — a different
certificate type from both Apple Development and Apple Distribution. Without it, a downloaded
copy is blocked by Gatekeeper, and on current macOS the old right-click → Open bypass is gone;
the recipient has to go to Privacy & Security and click "Open Anyway".

The App Store is not an option. The sandbox forbids both global hotkeys and injecting
keystrokes into other applications.

## If you already installed the CLI hotkeys

The app and `skhd` would both answer ⌘⌥B. One press would start two recordings and produce
two transcripts, so pick one. The app notifies you at launch when it detects this.

Either drop the CLI bindings, which leaves the rest of your `~/.skhdrc` alone:

```bash
sed -i '' '/# >>> voicemode hotkeys >>>/,/# <<< voicemode hotkeys <<</d' ~/.skhdrc
skhd --reload
```

Or stop skhd altogether, if voicemode was the only thing you used it for:

```bash
skhd --stop-service
```

## Using it

The menu-bar icon shows the state: a microphone when idle, a filled record dot while
recording, a waveform while transcribing or speaking.

| Shortcut | Action |
| --- | --- |
| ⌘⌥B | Start dictation; press again to stop, transcribe and insert |
| ⌘⌥T | Speak the selected text; press again to stop |

The menu also offers the model (`tiny`, `base`, `small`, `medium`), a **Language** submenu, and
**Copy Last Transcript** for when insertion failed or went somewhere unexpected.

Defaults are `small` with English pinned, which transcribes better than the CLI's `base`
default and avoids Whisper inventing text in another language when handed near-silence. A
recording shorter than 0.3 seconds is discarded for the same reason.

### Languages

The Language submenu covers English, Vietnamese, Chinese, French, German, Japanese, Korean and
Spanish, plus **Auto-detect**. Pinning a language is worth doing: it is faster than detection
and stops a quiet recording being read as some other language entirely.

Whisper itself handles 100 languages. The menu lists a few; the CLI takes any code through
`VOICE_STT_LANGUAGE`, and the list in the menu is `LANGUAGE_CHOICES` at the top of
`voiceapp.py` if you want others.

Larger models are better at non-English audio, and the gap is wider than it is for English. If
a language transcribes poorly on `small`, try `medium` before concluding it does not work.

**This setting is for transcription only.** Kokoro, the bundled TTS engine, speaks English and
has no Vietnamese voice, so ⌘⌥T reads text in English regardless of what the Language submenu
says. macOS does ship a Vietnamese system voice (`Linh`), reachable with `say -v Linh`, but
voicemode has no backend for it yet.

Preferences are stored in `NSUserDefaults` under the bundle id, not in the environment
variables the CLI reads.

## What is in the bundle

Python 3.11 and the inference stack, which is the bulk of it. Model weights are **not**
bundled: Whisper downloads on first launch, and Kokoro's voices download the first time TTS is
used. Bundling both would have added nearly 500 MB to the download.

The optional Edge TTS backend is excluded from the app. It pulls 33 MB of locale data for a
feature that sends text to Microsoft's servers, which is a poor trade in a local-first tool.
It remains available in the CLI.

## The log

The app writes to `voiceapp.log` in its state directory, normally
`/tmp/voice-stt-$UID/voice-stt/voiceapp.log`, and **Open Log** in the menu opens it. Each launch
records the model, the language and the resolved text encoding; each dictation records the
transcript; failures record a traceback.

The encoding line is there for a reason. A bundle does not get the locale a terminal does, and
anything that converts text without naming an encoding behaves differently inside the app than
it does from a shell. If non-ASCII text ever comes out wrong again, that line is the first
thing to read.

## Limitations

- Models download on first launch, so the first dictation needs a network connection.
- Transcription is CPU-only; `ctranslate2` has no Metal backend.
- The app and the CLI share `~/.skhdrc`-free state but not preferences, so settings changed in
  one are not seen by the other.
- If another application already owns ⌘⌥B or ⌘⌥T, Carbon refuses the registration and the app
  says so in a notification rather than failing silently. The combination is not yet
  configurable from the menu.
