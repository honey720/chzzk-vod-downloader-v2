#!/usr/bin/env bash
# 리눅스 러너에 Qt 테스트용 시스템 패키지를 설치한다 — ci.yml · dev-push-test.yml이 부른다.
#
# ~/apt-debs에 .deb가 있으면(GitHub 캐시에서 복원한 것) dpkg로 설치한다 — Ubuntu 서버에 접속하지
# 않는다. 없거나 그 설치가 실패하면 apt로 받아 설치하고, 받은 .deb를 ~/apt-debs에 남긴다
# (dev-push-test.yml이 캐시에 저장한다). 이 파일이 바뀌면 캐시 키가 바뀌어 캐시를 새로 만든다.
set -euo pipefail

DEBS="${APT_DEBS_DIR:-$HOME/apt-debs}"
PACKAGES=(
  xvfb libegl1 libgl1 libxkbcommon-x11-0 libxcb-icccm4
  libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0
  libxcb-shape0 libxcb-xinerama0 libxcb-cursor0 libdbus-1-3
  fonts-dejavu-core
)
# 한글 폰트는 스크린샷 글자용이다 — 라틴 폰트(DejaVu)만 있어도 테스트 결과는 같다.
# 못 받으면 건너뛴다
OPTIONAL=(fonts-nanum)
# 받은 .deb를 DEBS에 둔다. 기본 자리(/var/cache/apt/archives)는 설치 뒤에 비우는 이미지가 있다
APT=(sudo apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30
     -o Dir::Cache::archives="$DEBS" -o APT::Sandbox::User=root)

all_installed() {
  local package
  for package in "${PACKAGES[@]}"; do
    [[ "$(dpkg-query -W -f='${db:Status-Abbrev}' "$package" 2>/dev/null)" == ii* ]] || return 1
  done
}

install_from_cache() {
  compgen -G "$DEBS/*.deb" > /dev/null || { echo "캐시 없음"; return 1; }
  echo "캐시의 .deb $(find "$DEBS" -maxdepth 1 -name '*.deb' | wc -l)개를 dpkg로 설치한다"
  sudo dpkg -i "$DEBS"/*.deb && all_installed
}

install_from_apt() {
  echo "apt로 받아 설치한다"
  sudo rm -rf "$DEBS"
  mkdir -p "$DEBS/partial"
  "${APT[@]}" update
  # 캐시 설치가 중간에 실패했으면 의존 패키지가 빠진 채 남는다 — 먼저 그것부터 채운다
  "${APT[@]}" install -y --no-install-recommends --fix-broken
  "${APT[@]}" install -y --no-install-recommends "${PACKAGES[@]}"
  sudo timeout 120 "${APT[@]:1}" install -y --no-install-recommends "${OPTIONAL[@]}" \
    || echo "::warning::${OPTIONAL[*]} 설치를 건너뛰었다(느린 미러)"
  # 캐시에 넣을 것은 .deb뿐이다 — apt가 만든 잠금 파일 · partial은 지우고 소유자를 되돌린다
  sudo rm -rf "$DEBS/partial" "$DEBS/lock"
  sudo chown -R "$(id -u):$(id -g)" "$DEBS"
  all_installed
}

install_from_cache || install_from_apt
echo "설치 끝 — $DEBS의 .deb $(find "$DEBS" -maxdepth 1 -name '*.deb' | wc -l)개"
