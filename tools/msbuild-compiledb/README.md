# MSBuild → compile_commands.json 로거

MSBuild(.vcxproj/.sln) 프로젝트에서 `compile_commands.json` 을 뽑는 로거입니다.
빌드 중 실제 `cl.exe` 호출을 관찰해서 기록하므로, MSVC 의 property sheet·매크로·
상속된 include 경로가 전부 반영된 정확한 결과가 나옵니다.

**직접 쓸 일은 없습니다.** `python -m crex compiledb` 가 알아서 빌드하고 실행합니다.
여기 있는 것은 그 명령이 쓰는 재료입니다.

## 무엇이 들어 있나

| 파일 | 출처 |
|---|---|
| `CompileCommandsJson.cs` | [0xabu/MsBuildCompileCommandsJson](https://github.com/0xabu/MsBuildCompileCommandsJson) `d9b1dec` (2023-11-20), **수정 없음** |
| `LICENSE` | 위 저장소의 MIT 라이선스 원본 |
| `CompileCommandsJson.crex.csproj` | CREX 가 추가한 빌드 파일 (아래 참고) |

## 왜 소스로 담나

컴파일된 DLL 을 담으면 반입 심사가 "이 바이너리가 무엇을 하는지"를 물어야 합니다.
소스는 300줄이고 사람이 읽어서 확인할 수 있습니다. CREX 가 Python 런타임까지
소스로 담는 것과 같은 이유입니다 — [`docs/transfer.md`](../../docs/transfer.md) 참고.

DLL 은 폐쇄망 안에서 처음 `crex compiledb` 를 돌릴 때 만들어져
`<저장소>/.crex/compiledb/` 에 캐시됩니다. 그 뒤로는 다시 만들지 않습니다.

## 왜 csproj 를 따로 두나

상류의 csproj 는 SDK 스타일이라 `dotnet build` 와 NuGet 복원이 필요합니다.
폐쇄망에서 제일 먼저 막히는 조합입니다. `CompileCommandsJson.crex.csproj` 는
같은 소스를 옛 형식으로 빌드합니다 — PackageReference 가 없어 복원이 일어나지
않고, MSBuild 어셈블리는 `$(MSBuildToolsPath)` 로 그 VS 것을 그대로 씁니다.
결과적으로 **빌드에 필요한 것이 MSBuild 하나**입니다.

## 알아둘 제약

관찰 방식이라 **빌드되지 않은 파일은 기록되지 않습니다.** 증분 빌드면 그때 컴파일된
것만 들어갑니다. 그래서 `crex compiledb` 는 기본값으로 `/t:Rebuild` 를 씁니다.
큰 솔루션에서는 시간이 걸리는데, 대신 결과가 정확합니다.

로거는 빌드 도중 파일에 조금씩 써 나가므로 중간 상태를 다른 도구가 읽으면 깨진
JSON 을 봅니다. `crex compiledb` 는 임시 파일에 받아 성공했을 때만 제자리로
옮깁니다.
