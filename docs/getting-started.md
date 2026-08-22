# 시작하기

## 준비물

Python 3.11 이상이면 됩니다. 3.11부터 `tomllib` 이 표준 라이브러리에 들어왔고,
CLI 는 그것 말고 외부 패키지를 쓰지 않습니다. **`pip install` 없이 바로 씁니다.**

```
python --version
```

3.10 이하라면 동작하지 않습니다. `tomllib` 대신 `tomli` 를 넣는 식으로 우회할
수는 있지만 권장하지 않습니다.

Zed 에이전트 패널에서 부르려면(MCP) 그때만 설치가 필요합니다.

```bash
pip install -r requirements.txt
```

리뷰 로직은 같습니다. MCP 는 그 앞에 붙는 얇은 껍데기일 뿐이라 나중에 붙여도
됩니다. 설정은 [운영](operations.md#zed-연동-mcp)에 있습니다.

그리고 vLLM 이 떠 있어야 합니다. 아직 없다면 [운영](operations.md#vllm-기동)의
기동 명령을 먼저 보세요. 일단 설정 검증까지는 vLLM 없이도 진행할 수 있습니다.

## 설정 파일

예시를 복사해서 씁니다.

```bash
cp crex.example.toml crex.toml
```

처음에는 엔드포인트 주소와 모델 이름만 고치면 됩니다. 나머지 기본값은 그대로
두세요. 특히 `max_input_tokens = 8192` 는 손대지 마십시오 — 왜 그런지는
[설정](configuration.md#입력-토큰-상한을-왜-8192-로-두나)에 적어뒀습니다.

```toml
[llm.generator]
base_url = "http://vllm-qwen:8000/v1"
model = "Qwen3.6-27B"

[llm.verifier]
base_url = "http://vllm-gemma:8000/v1"
model = "gemma-4-26b-it"
```

vLLM 인스턴스가 하나뿐이라면 `[llm.verifier]` 블록을 통째로 지우세요. 그러면
생성 쪽 설정을 재사용합니다. 교차 모델 검증의 이점은 사라지지만 파이프라인은
돕니다. GPU 가 확보되는 대로 두 번째 인스턴스를 띄우는 걸 권합니다.

`crex.toml` 은 현재 디렉터리에서 위로 올라가며 찾습니다. 저장소 루트에 두면
하위 어디서 실행해도 잡힙니다. `--workspace` 로 다른 저장소를 지정했다면 그
저장소 안의 `crex.toml` 을 먼저 봅니다 — 아래 [CREX 는 어디에 두나](#crex-는-어디에-두나)를
보세요.

## 첫 점검

```bash
python -m crex doctor
```

이게 첫 명령입니다. 무엇이 준비됐고 무엇이 빠졌는지 한 화면에 보여줍니다.

```
워크스페이스: D:\work\myrepo
  출처=--workspace git=OK 리포트=D:\work\myrepo\reports

설정 파일: D:\work\myrepo\crex.toml
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

정적분석 도구와 tree-sitter 가 "없음"이어도 리뷰는 됩니다. 다만 품질이 떨어지니
가능하면 채우세요. LLM 엔드포인트가 실패하면 그건 진짜 막힌 겁니다 —
[문제 해결](troubleshooting.md#llm-엔드포인트-연결-실패)을 보세요.

`doctor` 는 엔드포인트가 하나라도 실패하면 종료 코드 1을 냅니다. 설치 스크립트에
넣어두면 유용합니다.

## CREX 는 어디에 두나

**리뷰 대상 저장소 안에 둘 필요가 없습니다.** 설치본은 한 자리에 두고 대상만
가리킵니다. 폐쇄망에서는 이게 중요합니다 — 저장소마다 복사해 두면 어느 것이
반입 심사를 통과한 사본인지 알 수 없게 됩니다.

```
D:\tools\crex\        ← 설치본. 여기서 실행합니다
D:\work\myrepo\.git   ← 리뷰 대상
D:\work\other\.git    ← 이것도 같은 설치본으로 봅니다
```

```bash
cd D:\tools\crex
python -m crex review --workspace D:\work\myrepo --staged
```

매번 치기 싫으면 명령 한 줄로 `crex.toml` 에 고정합니다.

```bash
python -m crex workspace D:\work\myrepo     # 고정
python -m crex workspace                    # 지금 무엇을 보고 있나
python -m crex workspace --clear            # 해제
```

직접 적어도 됩니다.

```toml
workspace = "D:/work/myrepo"
```

```powershell
$env:CREX_WORKSPACE = "D:\work\myrepo"
```

저장소마다 설정이 다르다면(C++ 프로젝트의 `compile_commands_dir`, C# 의
`dotnet_project` 등) 각 저장소 루트에 `crex.toml` 을 두세요. `--workspace` 로
지정하면 그 파일을 먼저 씁니다. 전체 규칙은 [설정](configuration.md#workspace--리뷰-대상-저장소)에
있습니다.

물론 예전처럼 저장소 안에서 실행해도 됩니다. 아무것도 지정하지 않으면 현재
디렉터리에서 git 루트를 찾습니다.

## 첫 리뷰

리뷰 대상 저장소의 작업 트리에 변경이 있는 상태에서:

```bash
python -m crex review
```

인자 없이 쓰면 `git diff HEAD` 를 봅니다. 커밋 전 변경 전체입니다.

스테이징된 것만 보려면:

```bash
python -m crex review --staged
```

두 커밋 사이를 보려면:

```bash
python -m crex review --from main --to HEAD
```

MR 리뷰라면 이 형태를 씁니다. `--from` 에는 병합 기준점을 넣으세요.
브랜치가 오래됐다면 `git merge-base main HEAD` 로 실제 분기점을 구해서 넣는 게
정확합니다. 안 그러면 남이 main 에 넣은 변경까지 리뷰 대상에 들어옵니다.

```bash
python -m crex review --from $(git merge-base main HEAD) --to HEAD
```

## 결과 읽기

기본 출력은 마크다운입니다.

```markdown
# 코드리뷰 결과

총 3건 (높음 1건, 중간 2건)

## 🔴 높음

### `src/buffer.cpp:16` — cpp.dangling-after-realloc

resize() 호출로 내부 버퍼가 재할당되면서 raw 포인터가 무효화됩니다.
16번 줄에서 그 포인터에 쓰면 해제된 메모리를 건드립니다.

​```
data_.resize(data_.size() + extra);
int* raw = &data_[0];   // resize 이후에 다시 얻는다
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

맨 아래 실행 통계를 습관적으로 보세요. **기각률이 40~60% 밖이면 뭔가 잘못된
신호입니다.** 너무 낮으면 검증이 일을 안 하는 것이고 (검증 모델이 무조건 yes 를
내는지 의심하세요), 너무 높으면 생성 쪽 프롬프트나 룰이 헛돌고 있는 겁니다.

지적이 하나도 없으면 "지적 사항 없음"만 나옵니다. 이건 정상이고 흔합니다.
CREX 는 확신이 없으면 침묵하도록 만들어져 있습니다.

## 파일로 받기

```bash
python -m crex review --from main --to HEAD --out reports/
```

```
markdown: reports\review.md
sarif: reports\review.sarif
json: reports\review.json
```

- **마크다운** — 사람이 읽습니다. MR 코멘트에 그대로 붙여넣으면 됩니다.
- **SARIF** — VS Code 의 SARIF Viewer 확장이나 품질 대시보드가 읽습니다.
- **JSON** — 기각된 지적까지 전부 들어 있습니다. 필터를 튜닝할 때 이걸 봅니다.

기각 내역은 마크다운에 안 나옵니다. 리뷰어가 볼 필요가 없으니까요. 대신 JSON 의
`rejected` 배열에 사유와 함께 전부 남습니다. 필터가 뭘 왜 걸렀는지 궁금할 때
여기를 여세요.

## 종료 코드

`high` 심각도 지적이 하나라도 있으면 1, 아니면 0입니다. 커밋 훅이나 스크립트에서
게이트로 쓸 수 있습니다.

```bash
python -m crex review --staged --out reports/ || echo "심각한 지적이 있습니다"
```

도입 초기에는 이걸 강제하지 마세요. 신뢰가 쌓이기 전에 커밋을 막으면 사람들이
`--no-verify` 로 우회하기 시작하고, 그러면 도구가 죽습니다.

## 전체 파일 감사

diff 없이 기존 코드를 통째로 볼 때 씁니다.

```bash
python -m crex scan src/legacy.cpp src/parser.cpp
```

파일을 400줄 창으로 자르되 함수 중간을 자르지 않도록 경계를 맞춥니다.
변경 라인이라는 개념이 없으므로 모든 줄이 지적 대상이고, 그만큼 오탐이 늘어납니다.
diff 리뷰보다 신뢰도가 낮다고 보시면 됩니다.

5000줄짜리 파일이면 청크가 13개 남짓 나오고, 청크당 LLM 호출이 두 번(생성+검증)
이므로 시간이 꽤 걸립니다. `--out` 으로 받아서 나중에 읽는 편이 낫습니다.

## 다음

- Zed 에서 쓰는 법 → [워크플로](workflow.md)
- 룰을 팀 코드에 맞게 손보려면 → [룰 작성법](writing-rules.md)
- 도입 효과를 숫자로 재려면 → [평가와 튜닝](evaluation.md)
- 폐쇄망에 옮기려면 → [운영](operations.md)
