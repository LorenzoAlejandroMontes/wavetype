<!-- DRAFT for the macOS section of README.md. Not linked yet: fill the download link and the
     data paths once the first notarized build exists and the Mac paths are final. -->

## Wavetype on Mac

Requires macOS 14 Sonoma or later on Apple silicon (M1 and newer).

### Install

1. Download **Wavetype-&lt;version&gt;-arm64.dmg** from the [latest release](https://github.com/LorenzoAlejandroMontes/wavetype/releases/latest).
2. Open it and drag **Wavetype** onto **Applications**.
3. Launch Wavetype from Applications. It lives in the background: no Dock icon.

### First launch

Wavetype asks for two permissions, one screen each:

- **Microphone**, to hear you.
- **Accessibility**, to read your hotkey from any app and paste the text where your cursor is.

Each row shows its status live and has a button that opens the right page in System Settings.
Then paste a free Groq key, or skip it and dictate with the built-in offline engine.

### Hotkeys

| Do this | To |
|---|---|
| Tap **Fn** (Globe) | Start dictating; tap again to stop and paste |
| Hold **Fn** | Talk while you hold it; let go to paste |
| **Ctrl+Option** | Same as Fn, for keyboards without an Apple Fn key |
| **Esc** | Cancel the current dictation |
| **Ctrl+Option+R** | Recover the last recording |
| **Ctrl+Option+T** | Switch the card style |
| **Ctrl+Option+Q** | Quit Wavetype |

Tip: so the Fn key doesn't open the emoji picker or dictation, set
System Settings > Keyboard > **Press fn key to: Do Nothing**.

### Build from source

```bash
bash packaging/mac/build.sh     # dist/Wavetype.app and dist/Wavetype-<version>-arm64.dmg
```

Needs Python 3.12 from python.org. Every push to the `macos` branch builds the app on GitHub
Actions, dictates a test sentence into TextEdit with it and saves screenshots of each step
(`.github/workflows/macos.yml`). Signing and notarization: `packaging/mac/SIGNING.md`.
