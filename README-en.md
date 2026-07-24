
[![한국어](https://img.shields.io/badge/한국어-클릭-yellow?style=flat-square)](README.md)
[![English](https://img.shields.io/badge/English-Click-yellow?style=flat-square)](README-en.md)
[![release](https://img.shields.io/github/v/release/honey720/chzzk-vod-downloader-v2?style=flat-square)](https://github.com/honey720/chzzk-vod-downloader-v2/releases)


<p align="center">
  <img src="resources/icon.png" alt="logo" width="128">
</p>

# Chzzk VOD Downloader v2

> A program for downloading Chzzk videos and clips.

![main](https://github.com/user-attachments/assets/ae01a231-e3d0-425c-a76f-0042d49a2a8b)  
---

## 📌 Features

- Supports **dynamic threading** to utilize your internet connection's full download speed.
- Allows **multiple VOD downloads** — add several VODs to the queue and download them simultaneously.
- **Resolution selection** lets you choose from various quality levels before downloading.
- **Cookie storage** allows access to age-restricted VODs.

![usage](https://github.com/user-attachments/assets/857b3cfc-dbb1-4e5b-a6f8-027eb48f2e35)

---

## 🚀 How to Use

1. **Add VOD**
   - Enter the VOD URL and press the **Add VOD** button or hit Enter to add it to the download queue.
   - Simply drag and drop Chzzk video, clip card or a list of URLs to add them.

2. **Select Resolution**
   - Click on a resolution button on the card to choose your desired quality.
   - The default setting is the highest available quality.

3. **Start Download**
   - Use the **Download/Pause** toggle button to start or pause downloads.
   - Click the **Stop** button to cancel the download.

4. **Change Settings**
   - Click the **Settings** button to save your cookies and access age-restricted content.
   - To use your selected **Language**, please restart the application after applying the setting.

---

## 💾 Download · Supported OS

Get the latest builds from the [Releases](https://github.com/honey720/chzzk-vod-downloader-v2/releases) page. Download the asset that matches your OS (`<version>` is the release tag, e.g. `v2.8.0`).

| OS | Support | File to download |
|---|---|---|
| Windows | Windows 10 / 11 (x64) | `CVDv2-<version>-windows.exe` |
| macOS | **Apple Silicon (M1 or later) only — Intel Macs are not supported** | `CVDv2-<version>-macos-arm64.zip` |
| Linux | Ubuntu 22.04 or equivalent, newer (x64) | `CVDv2-<version>-linux` |

> If none of the above fits, or you can't use the prebuilt binaries, you can run the app directly from source — see [Running from Source](#-running-from-source-development) below.

---

## 🍎 Running on macOS

The distributed app is not code-signed, so on first launch Gatekeeper shows an "unidentified developer" warning and blocks it. Use one of the following to bypass it.

1. Unzip the downloaded `.zip` and move `CVDv2.app` into `Applications` (or wherever you like).
2. **Right-click (or Control-click) → Open → Open.** (Only needed once; afterwards a normal double-click works.)

Alternatively, remove the quarantine attribute from the terminal:

```bash
xattr -dr com.apple.quarantine /Applications/CVDv2.app
```

---

## 🛡 Antivirus False Positives

Executables compiled with Nuitka have no code signature or reputation data, so some antivirus engines (Windows Defender in particular) often **flag them via machine-learning heuristics**. This is a false positive caused by the compilation method, not actual malware.

- Every release attaches a **VirusTotal full-engine scan link** in the release notes, so you can review the results yourself.
- These builds have been **cleared as safe (harmless) by BitDefender Labs**.

---

## ⚠ Known Limitations

- **VODs protected with encryption (AES) are currently not supported for download.** This applies to some membership-only broadcasts and replays; the app handles such VODs with a "not supported" notice.

---

## 🛠 Running from Source (Development)

Dependencies are managed with [uv](https://docs.astral.sh/uv/). Python 3.13+ is required.

```bash
uv sync                  # install dependencies
uv run python main.py    # run the app
```

- When reporting a download issue: run `uv run python scripts/capture_playback_debug.py <VOD URL>` and attach the captured responses (cookies/tokens are removed automatically).
- Download without the GUI: `uv run python scripts/headless_download.py <VOD/clip URL> [--resolution N] [--output PATH] [--timeout SEC]`

---

## 📚 References
- This project was developed with reference to [chzzk-vod-downloader](https://github.com/24802/chzzk-vod-downloader).

---

## ⚠ Disclaimer
- **This is not a stable release.**
- The developer is not responsible for any damages or issues that may arise from using this program.

---

## 💡 Contact
If you have suggestions or encounter any issues, please submit them via [Issues](https://github.com/honey720/chzzk-vod-downloader-v2/issues).