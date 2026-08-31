/*
 * Presentation 계층 — 판정 표를 CSV 로 내보낸다.
 *
 * 서버를 거치지 않는다. 판정은 이미 화면 상태에 있고(진행 중인 실행은 이벤트로,
 * 지난 실행은 localStorage 기록으로), 서버에는 지난 실행이 남아 있지 않다.
 * 내보내기를 API 로 만들면 어제 돌린 실행은 내보낼 수 없게 된다.
 *
 * ## 이 파일이 따로 있는 이유
 *
 * 값을 CSV 한 칸에 넣는 일은 짧지만 함정이 많다. 표를 그리는 코드 사이에 끼워
 * 두면 다음에 열이 하나 늘 때 누군가 `join(',')` 한 줄로 다시 쓰게 된다.
 *
 * ## 이스케이프 규칙 (RFC 4180 + 현실)
 *
 * 1. **BOM** — 앞에 U+FEFF 를 붙인다. 없으면 Excel 이 UTF-8 파일을 시스템
 *    코드페이지(한국어 Windows 는 cp949)로 읽어 한글이 전부 깨진다. 대상
 *    사용자가 바로 그 환경이다.
 * 2. **줄 끝은 CRLF** — RFC 4180 이 그렇고, 메모장으로 열어도 줄이 붙지 않는다.
 * 3. **따옴표·쉼표·줄바꿈이 든 칸은 큰따옴표로 감싸고, 안의 `"` 는 `""` 로
 *    두 번 쓴다.** 지적 본문에는 `"const char*"` 같은 인용이 흔하고, 수정안은
 *    여러 줄짜리 코드다.
 * 4. **칸 안의 줄바꿈은 LF 로 통일한다.** 감싼 칸 안의 CRLF 는 규격상 정당하지만
 *    줄 끝과 바이트열이 같아, 따옴표를 보지 않는 엉성한 파서에서 행이 하나 더
 *    생긴 것처럼 보인다. 파일 안에서 CRLF 는 행 구분자 하나뿐이게 둔다.
 * 5. **제어문자는 버린다.** NUL 하나가 파일 전체를 못 읽게 만든다. 줄바꿈과
 *    탭은 남긴다.
 * 6. **수식으로 시작하는 값 앞에 `'` 를 붙인다.** `=`, `+`, `-`, `@` 로 시작하는
 *    칸을 Excel 은 수식으로 해석한다. 여기 들어가는 값은 모델이 쓴 코드라
 *    `-1` 이나 `= nullptr` 로 시작하는 일이 실제로 있고, 그 칸은 `#NAME?` 이
 *    되어 원문이 사라진다. 남이 만든 CSV 를 여는 쪽에서는 그 이상으로,
 *    셀 하나가 외부 명령을 부르는 공격 통로가 된다.
 */

window.CREX = window.CREX || {};

CREX.csv = (function () {
  'use strict';

  const BOM = '\uFEFF';   // 눈에 보이지 않는 글자라 이스케이프로 적는다
  const EOL = '\r\n';

  const FORMULA_LEAD = /^[=+\-@]/;
  const CONTROL = /[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g;
  // 감싸야 하는 조건: 구분자·따옴표·줄바꿈이 들어 있거나, 앞뒤가 공백이거나
  // (엉성한 파서가 잘라먹는다), 수식 방지 접두어를 붙였거나.
  const MUST_QUOTE = /[",\n]|^['\s]|\s$/;

  const KEPT_LABEL = { true: '유지', false: '기각' };
  const YES_NO = { true: '예', false: '아니오' };

  /*
   * 열 정의. `head` 는 사람이 읽는 머리글이고 `get` 은 판정 행에서 값을 꺼낸다.
   *
   * 화면의 표보다 열이 많다. 화면은 훑어보는 곳이고 이 파일은 남겨서 세는
   * 물건이라, 필터를 다듬을 때 필요한 것(기각 사유, 결정론적 여부, 청크)을
   * 같이 담는다. rule_id 와 severity 는 한글 라벨이 아니라 원래 값 그대로 넣는다 —
   * 평가 리포트·플라이휠 통계와 이어 붙일 수 있어야 한다.
   */
  const COLUMNS = [
    { head: '파일', get: (row) => row.path },
    { head: '라인', get: (row) => row.line },
    { head: '끝라인', get: (row) => row.end_line },
    { head: '심각도', get: (row) => row.severity },
    { head: '차원', get: (row) => row.dimension },
    { head: '룰', get: (row) => row.rule_id },
    { head: '판정', get: (row) => KEPT_LABEL[!!row.kept] },
    { head: '기각사유', get: (row) => row.reject_reason || '' },
    { head: '결정론적기각', get: (row) => YES_NO[!!row.short_circuited] },
    { head: '내용', get: (row) => row.message },
    { head: '수정안', get: (row) => row.suggestion || '' },
    { head: '검증사유', get: (row) => row.reason || '' },
    { head: '검증코멘트', get: (row) => row.verifier_comment || '' },
    { head: '청크', get: (row) => row.chunk_id || '' },
  ];

  /* 값 하나를 CSV 한 칸으로 만든다. 규칙은 파일 첫머리에 적어 두었다. */
  function field(value) {
    let text = value === null || value === undefined ? '' : String(value);
    text = text.replace(CONTROL, '').replace(/\r\n?/g, '\n');
    if (FORMULA_LEAD.test(text)) text = "'" + text;
    if (MUST_QUOTE.test(text)) text = '"' + text.replace(/"/g, '""') + '"';
    return text;
  }

  function row(values) {
    return values.map(field).join(',');
  }

  /* 판정 행 목록 → CSV 문서 전체. */
  function build(rows, columns) {
    const cols = columns || COLUMNS;
    const lines = [row(cols.map((col) => col.head))];
    (rows || []).forEach((item) => {
      lines.push(row(cols.map((col) => col.get(item))));
    });
    // 마지막 줄에도 줄바꿈을 둔다. 없으면 붙여 쓰는 도구가 헤더에 이어 붙인다.
    return BOM + lines.join(EOL) + EOL;
  }

  /* `crex-verdicts-20260831-1420-kept.csv` 처럼 만든다. */
  function filename(prefix, suffix) {
    const now = new Date();
    const pad = (value) => (value < 10 ? '0' : '') + value;
    const stamp = now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) +
      '-' + pad(now.getHours()) + pad(now.getMinutes());
    return prefix + '-' + stamp + (suffix ? '-' + suffix : '') + '.csv';
  }

  /* Blob 으로 만들어 저장 대화상자를 띄운다. 네트워크를 타지 않는다. */
  function download(name, text) {
    const blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = name;
    anchor.rel = 'noopener';
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    // 바로 거두면 저장이 시작되기 전에 URL 이 사라지는 브라우저가 있다.
    window.setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  return { COLUMNS, field, build, filename, download };
})();
