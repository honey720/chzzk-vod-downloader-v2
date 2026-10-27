/**
 * i18n JSON 카탈로그 조회 (JS 쪽) — #212, Phase A4.
 *
 * app/i18n.py와 같은 계약을 따른다: 같은 translations/<lang>.json 파일을
 * 진실의 원천으로 삼는다. 이 파일은 카탈로그를 스스로 fetch하지 않는다 —
 * Python 쪽이 (Phase C에서) window.evaluate_js로 setCatalog()를 호출해
 * 주입하는 방식과, 정적 리소스로 직접 fetch하는 방식 둘 다 열어두기 위해
 * "주입받는" 형태로만 설계했다. 어느 쪽을 쓸지는 Phase C(뷰 통합)에서 정한다.
 *
 * 빌드 스텝 없음(#208 결정 — 바닐라 JS 무빌드) — 이 파일은 <script> 태그로
 * 그대로 로드된다.
 */

(function (global) {
  "use strict";

  var _catalog = null;

  /**
   * 카탈로그를 설정한다. Python 쪽 app/i18n.py의 _load_catalog()가 읽는
   * translations/<lang>.json과 정확히 같은 {원문: 번역} 평평한 객체를 받는다.
   */
  function setCatalog(catalogObject) {
    _catalog = catalogObject || {};
  }

  /**
   * key를 조회한다. 카탈로그가 없거나 키가 없으면 원문(key) 그대로 반환한다 —
   * app/i18n.py의 translate()와 동일한 폴백 원칙(예외 대신 원문 노출).
   */
  function translate(key) {
    if (!_catalog) {
      return key;
    }
    return Object.prototype.hasOwnProperty.call(_catalog, key) ? _catalog[key] : key;
  }

  var i18n = { setCatalog: setCatalog, translate: translate };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = i18n; // 이 프로젝트에 Node 테스트 러너는 없다 — 향후를 위한 순수 호환성
  } else {
    global.i18n = i18n;
  }
})(typeof window !== "undefined" ? window : this);
