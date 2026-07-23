
[![한국어](https://img.shields.io/badge/한국어-클릭-yellow?style=flat-square)](README.md)
[![English](https://img.shields.io/badge/English-Click-yellow?style=flat-square)](README-en.md)
[![release](https://img.shields.io/github/v/release/honey720/chzzk-vod-downloader-v2?style=flat-square)](https://github.com/honey720/chzzk-vod-downloader-v2/releases)


# 치지직 VOD 다운로더 v2

> 치지직 다시보기 동영상, 클립을 다운로드하는 프로그램입니다.

![main](https://github.com/user-attachments/assets/ae01a231-e3d0-425c-a76f-0042d49a2a8b)
---

## 📌 특징

- **동적 스레드** 기술을 활용하여 사용자 인터넷 회선의 최대 속도로 다운로드할 수 있습니다.
- **다중 VOD 다운로드**를 지원하여 여러 VOD를 추가하고 다운로드합니다.
- **해상도 선택**버튼으로 다양한 해상도를 선택하여 다운로드합니다.
- **쿠키 저장** 기능으로 연령 제한이 있는 VOD에 접근 가능합니다.

![usage](https://github.com/user-attachments/assets/857b3cfc-dbb1-4e5b-a6f8-027eb48f2e35)

---

## 🚀 사용법

1. **VOD 추가**
   - VOD URL을 입력하고 **VOD 추가** 버튼을 클릭 or 엔터 입력 시 대기열에 메타데이터 카드가 추가됩니다.
   - 치지직 다시보기, 클립 카드를 드래그하거나 메모장에 작성한 URL 목록을 드래그해 추가할 수 있습니다.

2. **해상도 선택**
   - 추가된 카드에서 원하는 해상도 버튼을 클릭해 다운로드 해상도를 설정합니다.
   - 기본 설정은 최상의 품질 해상도입니다.

3. **다운로드 시작**
   - **다운로드/정지** 토글 버튼을 클릭하여 다운로드를 시작하거나 정지합니다.
   - **중지** 버튼을 클릭하여 다운로드를 중단합니다.

4. **설정 변경**
   - **설정** 버튼을 클릭하여 쿠키를 저장하고 연령 제한 콘텐츠에 접근할 수 있습니다.
   - 원하는 **언어**를 사용하기 위해서 적용 후 재시작하세요.

---

## 💾 다운로드 · 지원 OS

최신 실행 파일은 [Releases](https://github.com/honey720/chzzk-vod-downloader-v2/releases) 페이지에서 받을 수 있습니다. OS에 맞는 자산을 내려받으세요 (`<버전>`은 릴리즈 태그, 예: `v2.8.0`).

| OS | 지원 범위 | 받을 파일 |
|---|---|---|
| Windows | Windows 10 / 11 (x64) | `CVDv2-<버전>-windows.exe` |
| macOS | **Apple Silicon(M1 이상) 전용 — 인텔 맥 미지원** | `CVDv2-<버전>-macos-arm64.zip` |
| Linux | Ubuntu 22.04 이상 상당 (x64) | `CVDv2-<버전>-linux` |

> 위 환경에 해당하지 않거나 실행 파일을 쓸 수 없다면, 아래 [소스에서 실행](#-소스에서-실행-개발) 방법으로 직접 구동할 수 있습니다.

---

## 🍎 macOS 실행 안내

배포 앱은 코드 서명이 되어 있지 않아, 처음 실행할 때 Gatekeeper가 "확인되지 않은 개발자" 경고를 띄우고 실행을 막습니다. 다음 중 한 방법으로 우회하세요.

1. 내려받은 `.zip`을 풀고 `CVDv2.app`을 `응용 프로그램` 폴더 등으로 옮깁니다.
2. **우클릭(또는 Control-클릭) → 열기 → 열기** 를 선택합니다. (첫 1회만 필요하며, 이후에는 더블클릭으로 실행됩니다.)

또는 터미널에서 격리 속성을 제거해도 됩니다:

```bash
xattr -dr com.apple.quarantine /Applications/CVDv2.app
```

---

## 🛡 백신 오탐 안내

Nuitka로 컴파일된 실행 파일은 코드 서명·평판 정보가 없어, 일부 백신(특히 Windows Defender)이 **머신러닝 휴리스틱으로 오탐**하는 경우가 흔합니다. 실제 악성 코드가 아니라 컴파일 방식에서 비롯된 오진입니다.

- 릴리즈마다 릴리즈 본문에 **VirusTotal 전체 엔진 검사 링크**를 첨부하니, 직접 검사 결과를 확인할 수 있습니다.
- 본 실행 파일은 **BitDefender Labs로부터 안전(무해) 판정**을 받았습니다.

---

## ⚠ 알려진 제한

- **암호화(AES) 보호가 적용된 VOD는 현재 다운로드를 지원하지 않습니다.** 일부 멤버십 전용·중계 다시보기 등에 적용되어 있으며, 이런 VOD는 앱에서 "미지원" 안내로 처리됩니다.

---

## 🛠 소스에서 실행 (개발)

의존성은 [uv](https://docs.astral.sh/uv/)로 관리합니다. Python 3.13 이상이 필요합니다.

```bash
uv sync                  # 의존성 설치
uv run python main.py    # 앱 실행
```

- 다운로드 문제 제보 시: `uv run python scripts/capture_playback_debug.py <VOD URL>` 로 응답을 캡처해 첨부해 주세요 (쿠키·토큰은 자동 제거됩니다).
- GUI 없이 다운로드: `uv run python scripts/headless_download.py <VOD/클립 URL> [--resolution N] [--output PATH] [--timeout SEC]`

---

## 📚 참고 자료
- 본 프로그램은 [chzzk-vod-downloader](https://github.com/24802/chzzk-vod-downloader)를 참고하여 개발되었습니다.

---

## ⚠ 주의사항
- **안정적인 버전이 아닙니다.**
- 본 프로그램을 사용하는 과정에서 발생할 수 있는 피해에 대해 개발자는 책임을 지지 않습니다.

---

## 💡 문의
이 프로젝트에 대한 제안 사항이 있거나 문제를 발견하셨다면 [Issue](https://github.com/honey720/chzzk-vod-downloader-v2/issues)에 등록해 주세요.
