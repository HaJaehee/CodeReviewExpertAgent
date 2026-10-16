# 시작하기

## 준비물

Python 3.11 이상이면 충분합니다. 룰 택소노미(`rules/taxonomy.toml`)를 읽는 `tomllib` 모듈이 Python 3.11부터 표준 라이브러리에 포함되었으며, 코어 CLI는 그 외에 어떠한 외부 패키지도 사용하지 않습니다. **`pip install` 없이 바로 실행할 수 있습니다.**

```
python --version
```

Python 3.10 이하에서는 동작하지 않습니다. `tomllib` 대신 외부 `tomli` 패키지를 설치해 우회하는 방식은 권장하지 않습니다. (설정 파일은 JSON 형식이므로 이 제약과 무관합니다.)

Zed 에이전트 패널과 연동(MCP 서버)할 때만 추가 패키지 설치가 필요합니다.

```bash
pip install -r requirements.txt
```

핵심 리뷰 로직은 동일합니다. MCP 서버는 파이프라인 앞에 붙는 얇은 연동 계층일 뿐이므로 나중에 설치해도 됩니다. 연동 설정은 [운영](operations.md#zed-연동-mcp) 문서를 참고하십시오.

또한 사내망에 vLLM 서버가 기동되어 있어야 합니다. 아직 준비되지 않았다면 [운영](operations.md#vllm-기동) 문서의 기동 명령을 먼저 확인하십시오. 설정 파일 검증까지는 vLLM 서버 없이도 진행할 수 있습니다.

정적분석 도구(`clang-tidy`, `cppcheck`, `ruff` 등)는 **설치되어 있지 않아도 리뷰가 동작합니다.** 다만 정적분석 도구가 있으면 지적의 사실적 근거가 보강되어 오탐이 크게 줄어듭니다. 설치는 나중에 진행해도 되므로, 우선은 `doctor` 명령으로 어떤 도구가 빠져 있는지만 확인하고 넘어가십시오 — 다운로드 경로와 라이선스는 [정적분석 도구](analyzers.md) 문서에 정리되어 있습니다.

## 설정 파일

예시 파일을 복사해서 사용합니다.

```bash
cp crex.example.json crex.json
```

처음에는 엔드포인트 주소와 모델 이름만 환경에 맞게 수정하면 됩니다. 나머지 기본값은 그대로 두십시오. 특히 `max_input_tokens`의 기본값 `8192`는 임의로 올리지 마십시오 — 그 이유는 [설정](configuration.md#입력-토큰-상한을-왜-8192-로-두나) 문서에 자세히 설명되어 있습니다.

```json
{
  "llm": {
    "generator": {
      "base_url": "http://vllm-qwen:8000/v1",
      "model": "Qwen3.6-27B"
    },
    "verifier": {
      "base_url": "http://vllm-gemma:8000/v1",
      "model": "gemma-4-26b-it"
    }
  }
}
```

예시 파일에서 `"// ..."`로 시작하는 키는 설명용 주석입니다. 표준 JSON에는 주석 문법이 없기 때문에 설명을 키-값 데이터 형태로 적어 둔 것입니다 — 삭제해도 되고 그대로 두어도 동작에는 아무런 영향이 없습니다.

만약 실행 중인 vLLM 인스턴스가 하나뿐이라면 `verifier` 항목을 통째로 지우십시오. 생략하면 생성 모델(`generator`) 설정을 검증 단계에서도 함께 사용합니다. 교차 모델 검증의 이점은 줄어들지만 파이프라인은 정상 동작합니다. 이후 GPU 자원이 확보되는 대로 두 번째 인스턴스를 분리해 띄우는 것을 권장합니다.

`crex.json`은 현재 작업 디렉터리에서 상위 디렉터리로 올라가며 자동으로 탐색합니다. 저장소 루트에 두면 하위 어느 폴더에서 실행하더라도 자동으로 인식됩니다. `--workspace` 옵션으로 다른 저장소를 지정한 경우에는 대상 저장소 안의 `crex.json`을 우선 사용합니다 — 아래 [CREX 는 어디에 두나](#crex-는-어디에-두나) 절을 참고하십시오.

## 첫 점검

```bash
python -m crex doctor
```

가장 먼저 실행해 볼 명령입니다. 현재 환경에서 무엇이 준비되었고 무엇이 빠져 있는지 한 화면에 보여줍니다.

```
워크스페이스: D:\work\myrepo
  출처=--workspace git=OK 리포트=D:\work\myrepo\reports

설정 파일: D:\work\myrepo\crex.json
  모드=native 생성=Qwen3.6-27B@http://vllm-qwen:8000/v1 검증=gemma-4-26b-it@... 입력상한=8192토큰 그라운딩=on

택소노미
  OK  v0.1.0, 룰 41개

LLM 엔드포인트
  OK  생성: Qwen3.6-27B @ http://vllm-qwen:8000/v1
       ok
  OK  검증: gemma-4-26b-it @ http://vllm-gemma:8000/v1
       ok

정적분석 도구
  OK  clang-tidy (clang-tidy)
  없음 cppcheck (cppcheck)
  OK  roslyn (dotnet)
  OK  ruff (ruff)
  없음 mypy (mypy)
  없음 bandit (bandit)

tree-sitter (선택)
  없음 tree_sitter — 휴리스틱 폴백으로 동작한다
  ...
```

정적분석 도구와 `tree-sitter`가 `없음`으로 표시되어도 리뷰는 실행됩니다. 다만 리뷰 정밀도를 높이려면 가급적 설치하는 것이 좋습니다 — 다운로드 방법과 라이선스 정보는 [정적분석 도구](analyzers.md) 문서를 참고하십시오. 반면 LLM 엔드포인트가 `실패`로 나오면 리뷰를 진행할 수 없습니다 — [문제 해결](troubleshooting.md#llm-엔드포인트-연결-실패) 문서를 확인하십시오.

`doctor`는 LLM 엔드포인트 중 하나라도 실패하면 종료 코드 `1`을 반환하므로, 배포 및 설치 자동화 스크립트에서 상태 확인용으로 활용하기 좋습니다.

## CREX 는 어디에 두나

**리뷰 대상 저장소 안에 CREX를 복사해 둘 필요가 없습니다.** CREX 설치본은 한곳에 두고 리뷰할 대상 저장소 경로만 지정하면 됩니다. 폐쇄망 환경에서는 이 원칙이 특히 중요합니다 — 저장소마다 코드를 복사해 두면 어느 폴더가 보안 반입 심사를 통과한 원본인지 관리하기 어려워지기 때문입니다.

```
D:\tools\crex\        ← 설치본 위치. 여기서 명령을 실행합니다
D:\work\myrepo\.git   ← 리뷰 대상 저장소 1
D:\work\other\.git    ← 리뷰 대상 저장소 2 (같은 설치본으로 리뷰)
```

```bash
cd D:\tools\crex
python -m crex review --workspace D:\work\myrepo --staged
```

매번 `--workspace`를 입력하기 번거롭다면 명령 한 줄로 `crex.json`에 기본 대상 저장소를 고정할 수 있습니다.

```bash
python -m crex workspace D:\work\myrepo     # 대상 저장소 고정
python -m crex workspace                    # 현재 설정된 대상 저장소 확인
python -m crex workspace --clear            # 고정 해제
```

`crex.json` 파일에 직접 작성하거나 환경변수로 지정해도 됩니다.

```json
{
  "workspace": "D:/work/myrepo"
}
```

```powershell
$env:CREX_WORKSPACE = "D:\work\myrepo"
```

C++ 저장소라면 최초 1회 `python -m crex compiledb`를 실행하십시오. `compile_commands.json`을 생성하고 그 경로를 설정 파일에 자동으로 등록합니다 — 이 파일이 없으면 `clang-tidy`가 헤더 경로를 찾지 못해 분석 정확도가 크게 떨어집니다 ([자세히 보기](analyzers.md#compile_commandsjson-이-없으면-반쯤-눈을-감습니다)).

저장소마다 설정이 다르다면(예: C++ 프로젝트의 `compile_commands_dir`, C# 프로젝트의 `dotnet_project` 등) 각 저장소 루트에 `crex.json`을 두십시오. `--workspace`로 대상을 지정하면 해당 저장소 안의 `crex.json`을 우선 적용합니다. 상세한 탐색 우선순위는 [설정](configuration.md#workspace--리뷰-대상-저장소) 문서를 참고하십시오.

물론 대상 저장소 디렉터리 안에서 직접 실행해도 됩니다. 별도로 워크스페이스를 지정하지 않으면 현재 디렉터리에서 상위로 올라가며 `.git` 루트를 자동으로 찾습니다.

## 첫 리뷰

리뷰 대상 저장소의 작업 트리에 수정 사항이 있는 상태에서 다음 명령을 실행합니다.

```bash
python -m crex review
```

별도 옵션 없이 실행하면 `git diff HEAD`(커밋되지 않은 작업 트리 변경 전체)를 리뷰합니다.

스테이징(`git add`)된 변경만 리뷰하려면:

```bash
python -m crex review --staged
```

두 커밋 또는 브랜치 사이를 비교하려면:

```bash
python -m crex review --from main --to HEAD
```

MR(Merge Request) 리뷰에는 이 형식을 사용합니다. `--from`에는 병합 기준 브랜치를 지정하십시오. 브랜치를 분기한 지 오래되었다면 `git merge-base main HEAD`로 실제 공통 분기점을 구해 넣는 것이 정확합니다. 그렇지 않으면 다른 개발자가 `main`에 병합한 변경까지 리뷰 범위에 포함될 수 있습니다.

```bash
python -m crex review --from $(git merge-base main HEAD) --to HEAD
```

## 결과 읽기

기본 출력 형식은 마크다운입니다.

```markdown
# 코드리뷰 결과

총 3건 (높음 1건, 중간 2건)

## 🔴 높음

### `src/buffer.cpp:16` — cpp.dangling-after-realloc

resize() 호출로 내부 버퍼가 재할당되면서 raw 포인터가 무효화됩니다.
16번 줄에서 해당 포인터에 값을 쓰면 해제된 메모리를 참조하게 됩니다.

​```
data_.resize(data_.size() + extra);
int* raw = &data_[0];   // resize 이후에 포인터를 다시 취득합니다
raw[0] = 42;
​```

## 🟡 중간
...

---

<details><summary>실행 통계</summary>

- 리뷰한 청크: 7개
- 정적분석 결과: 4건
- 생성된 지적: 8건 → 검증 통과 3건 (기각률 62.5%)
- 소요: chunk 0.3s / ground 12.1s / generate 41.7s / filter 8.4s

</details>
```

리포트 하단의 실행 통계를 주기적으로 확인하는 것이 좋습니다. **검증 기각률이 40~60% 범위를 크게 벗어나면 점검이 필요하다는 신호입니다.** 기각률이 지나치게 낮다면 검증 모델이 제 역할을 하지 못하고 무조건 통과(`yes`)시키고 있는지 확인해야 하며, 반대로 지나치게 높다면 생성 모델의 프롬프트나 룰 설정에서 과도한 오탐이 발생하고 있는 것입니다.

발견된 결함이 하나도 없으면 `"지적 사항 없음"`만 출력됩니다. 이는 매우 정상적이고 흔한 결과입니다. CREX는 확신이 없는 지적은 내보내지 않도록 설계되어 있습니다.

## 파일로 받기

```bash
python -m crex review --from main --to HEAD --out reports/
```

```
markdown: reports\review.md
sarif: reports\review.sarif
json: reports\review.json
html: reports\review.html
```

- **마크다운 (`.md`)** — 사람이 바로 읽기 좋은 형식입니다. MR 코멘트에 그대로 붙여넣어 활용할 수 있습니다.
- **SARIF (`.sarif`)** — VS Code의 SARIF Viewer 확장이나 정적분석 품질 대시보드에서 불러올 수 있는 표준 형식입니다.
- **JSON (`.json`)** — 최종 채택된 지적뿐만 아니라 검증 단계에서 기각된 지적(`rejected`)과 기각 사유까지 모두 담겨 있습니다. 필터와 룰을 튜닝할 때 활용합니다.
- **HTML (`.html`)** — 관제 화면(`crex.viz`)의 다크 테마 디자인과 접기/펼치기, 필터 탭을 그대로 담은 단일 파일 리포트입니다. 외부 네트워크 없이 브라우저에서 바로 열어볼 수 있습니다.

기각된 내역은 리뷰어의 피로도를 줄이기 위해 마크다운 리포트에는 표시하지 않습니다. 필터가 어떤 지적을 왜 걸러냈는지 확인하려면 JSON 리포트의 `rejected` 배열이나 HTML 리포트의 기각 탭을 확인하십시오.

## 종료 코드

| 코드 | 의미 |
|---|---|
| `0` | 정상 종료 (`high` 심각도 지적 없음) |
| `1` | `high` 심각도 지적이 1건 이상 발견됨 |
| `2` | 설정 또는 명령줄 인자 오류 (Git 저장소가 아님, 설정 키 오타 등) |
| `3` | **파이프라인 실행 도중 오류 발생** — 결과를 신뢰해서는 안 됩니다 |

Git 커밋 훅이나 CI 스크립트에서 품질 게이트로 활용할 수 있습니다.

```bash
python -m crex review --staged --out reports/ || echo "심각한 지적이 발견되었습니다"
```

종료 코드 `3`을 `0`과 엄격히 구분하는 이유가 있습니다. 만약 LLM 호출이 모두 실패하면 발견된 지적이 0건으로 집계되는데, 겉보기에는 "결함이 없는 깨끗한 코드"와 구분되지 않습니다. 이를 그대로 종료 코드 `0`으로 반환하면 고장 난 파이프라인이 모든 커밋을 정상 통과시키게 됩니다. 따라서 CI 스크립트에서는 종료 코드 `3`을 반드시 실패로 처리하십시오.

```bash
python -m crex review --staged --out reports/
case $? in
  0) echo "통과" ;;
  1) echo "심각한 지적이 있습니다" ;;
  3) echo "리뷰 실행 실패 — python -m crex doctor로 상태를 점검하십시오"; exit 1 ;;
esac
```

도입 초기에는 커밋 차단을 강제하지 않는 것이 좋습니다. 도구의 정밀도에 대한 팀의 신뢰가 쌓이기 전에 커밋을 강제로 막으면 개발자들이 `--no-verify`로 우회하기 시작하고, 결국 도구가 유명무실해집니다.

## 전체 파일 감사

diff 없이 기존 소스 파일 전체를 점검할 때 사용합니다.

```bash
python -m crex scan src/legacy.cpp src/parser.cpp
```

파일을 심볼 경계에 맞춰 최대 150줄 단위의 창(Window)으로 나누어 검사합니다. diff 리뷰와 달리 "이번에 변경된 라인"이라는 제약이 없으므로 모든 줄이 검사 대상이 되며, 그만큼 diff 리뷰보다 오탐이 더 많이 발생할 수 있습니다.

대형 파일을 스캔할 때는 청크 수가 많아져 시간이 걸리므로 `--out` 옵션으로 리포트를 저장한 뒤 확인하는 방식을 권장합니다.

## 다음

- Zed 에디터에서 대화형으로 사용하는 법 → [워크플로](workflow.md)
- 팀 코드베이스에 맞게 룰을 다듬는 법 → [룰 작성법](writing-rules.md)
- 도입 효과를 정량 지표로 측정하는 법 → [평가와 튜닝](evaluation.md)
- 폐쇄망에 반입하고 운영하는 법 → [운영](operations.md)
