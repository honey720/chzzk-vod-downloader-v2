
[![한국어](https://img.shields.io/badge/한국어-클릭-yellow?style=flat-square)](README.md)
[![English](https://img.shields.io/badge/English-Click-yellow?style=flat-square)](README-en.md)
[![release](https://img.shields.io/github/v/release/honey720/chzzk-vod-downloader-v2?style=flat-square)](https://github.com/honey720/chzzk-vod-downloader-v2/releases)


<p align="center">
  <img src="resources/icon.png" alt="logo" width="128">
</p>

# Chzzk VOD Downloader v2

> Download Chzzk VODs and clips.

![main](https://github.com/user-attachments/assets/ae01a231-e3d0-425c-a76f-0042d49a2a8b)  
---

## ✨ Features

- Downloads as fast as your connection allows.
- Queue up multiple videos and download them in one go.
- Pick the quality you want before downloading.
- Register your cookies to download age-restricted and members-only videos.

---

## 💾 Download

Grab the latest build from the [Releases](https://github.com/honey720/chzzk-vod-downloader-v2/releases) page and pick the file for your OS below (`<version>` is the release tag, e.g. `v2.9.0`).

| OS | Support | File to download |
|---|---|---|
| Windows | Windows 10 / 11 (x64) | `CVDv2-<version>-windows.exe` |
| macOS | **Apple Silicon (M1 or later) only — Intel Macs are not supported** | `CVDv2-<version>-macos-arm64.zip` |
| Linux | Ubuntu 22.04 or newer, or equivalent (x64) | `CVDv2-<version>-linux` |

> On a different setup? You can also run the app from source — see **Developer notes** below.

---

## 🚀 How to Use

1. **Add videos**
   - Paste a VOD or clip URL and click **Add VOD** (or press Enter) to add it to the queue.
   - You can also drag a video card straight from the Chzzk page, or drop in a whole list of URLs from a text file.

2. **Pick a quality**
   - Click a resolution button on the card. If you don't pick one, the highest quality is used.

3. **Download**
   - The **Download/Pause** button starts or pauses the queue; **Stop** cancels it.

4. **Settings & cookies**
   - Age-restricted and members-only videos can only be downloaded with the cookies of an account that can watch them. Register your cookies in **Settings**.
   - If you change the language, restart the app after applying.

![usage](https://github.com/user-attachments/assets/857b3cfc-dbb1-4e5b-a6f8-027eb48f2e35)

---

<details>
<summary><b>📖 User notes — first launch on macOS · antivirus false positives · disclaimer</b></summary>

#### 🍎 First launch on macOS

The app isn't code-signed, so macOS blocks it with an "unidentified developer" warning the first time you open it. You only need to allow it once. The exact steps vary between macOS versions, so follow Apple's official guide, **[Open a Mac app from an unknown developer](https://support.apple.com/guide/mac-help/open-a-mac-app-from-an-unidentified-developer-mh40616/mac)**. In short:

1. Open `CVDv2.app` and dismiss the warning.
2. Go to **System Settings → Privacy & Security** and click **Open Anyway** next to the CVDv2 entry.

If you prefer the terminal, clearing the quarantine flag also works:

```bash
xattr -dr com.apple.quarantine /Applications/CVDv2.app
```

#### 🛡 Antivirus flags the file

Executables built with Nuitka carry no code signature or reputation data, so some antivirus engines — Windows Defender in particular — often flag them as malicious. This is a false positive caused by how the app is compiled, not actual malware.

- Every release includes a **VirusTotal full-engine scan link** in the release notes, so you can check for yourself.
- The builds have been **verified as harmless by BitDefender Labs**.

#### ⚠ Disclaimer

- This is not a stable release yet.
- The developer is not responsible for any damage arising from the use of this program.

</details>

<details>
<summary><b>🛠 Developer notes — running from source · dev scripts · license</b></summary>

#### Running from source

Dependencies are managed with [uv](https://docs.astral.sh/uv/); Python 3.13 or newer is required.

```bash
uv sync                  # install dependencies
uv run python main.py    # run the app
```

#### Dev scripts

- When reporting a download problem, capture the server responses with `uv run python scripts/capture_playback_debug.py <VOD URL>` and attach the output — cookies and tokens are stripped automatically.
- To download without the GUI: `uv run python scripts/headless_download.py <VOD/clip URL> [--resolution N] [--output PATH] [--timeout SEC]`

#### License

- This program is distributed under the [GPL-3.0](LICENSE) license.
- Releases bundle an [FFmpeg](https://ffmpeg.org) executable used to remux the merged output
  — a GPL build shipped with the pip package [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) (BSD-2-Clause).
  FFmpeg is a product of the FFmpeg project; its source code is available from the
  [official FFmpeg repository](https://github.com/FFmpeg/FFmpeg) and the respective build providers.

</details>

---

## 💡 Feedback

Found a bug or have a suggestion? Please open an [issue](https://github.com/honey720/chzzk-vod-downloader-v2/issues).
