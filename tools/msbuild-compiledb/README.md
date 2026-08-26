# MSBuild → compile_commands.json 로거

MSBuild(.vcxproj/.sln) 프로젝트에서 `compile_commands.json` 을 뽑는 로거입니다.
빌드 중 실제 `cl.exe` 호출을 관찰해서 기록하므로, MSVC 의 property sheet·매크로·
상속된 include 경로가 전부 반영된 정확한 결과가 나옵니다.

**직접 쓸 일은 없습니다.** `python -m crex compiledb` 가 알아서 붙여 실행합니다.

## 무엇이 들어 있나

| 파일 | 출처 |
|---|---|
| `CompileCommandsJson.dll` | 아래 소스를 빌드한 것 (.NET Framework 4.7.2, Release) |
| `LICENSE` | [0xabu/MsBuildCompileCommandsJson](https://github.com/0xabu/MsBuildCompileCommandsJson) 의 MIT 라이선스 원본 |

DLL 은 그대로 실행됩니다. 폐쇄망 안에서 빌드하지 않습니다.

### 이 DLL 의 출처

- 소스: [0xabu/MsBuildCompileCommandsJson](https://github.com/0xabu/MsBuildCompileCommandsJson)
  `d9b1dec` (2023-11-20) 의 `CompileCommandsJson.cs`, 수정 없음
- 빌드: MSBuild 17.14.51, `TargetFrameworkVersion=v4.7.2`, `Configuration=Release`,
  `DebugType=none`. NuGet 복원 없음 — `Microsoft.Build.Framework` 와
  `Microsoft.Build.Utilities.Core` 를 `$(MSBuildToolsPath)` 로 참조합니다.
- 빌드일: 2026-08-27
- SHA-256: `95b138b1925af6e355961a3e53ada59204196a37e92c0a95b161e4b619ed84fb`

바이너리를 받지 않는 반입 심사라면 위 저장소에서 소스를 받아 같은 설정으로
다시 빌드하고, 나온 DLL 을 이 자리에 놓으면 됩니다. CREX 는 파일 이름
(`CompileCommandsJson.dll`)과 자리만 봅니다.

## 알아둘 제약

관찰 방식이라 **빌드되지 않은 파일은 기록되지 않습니다.** 증분 빌드면 그때 컴파일된
것만 들어갑니다. 그래서 `crex compiledb` 는 기본값으로 `/t:Rebuild` 를 씁니다.
큰 솔루션에서는 시간이 걸리는데, 대신 결과가 정확합니다.

로거는 빌드 도중 파일에 조금씩 써 나가므로 중간 상태를 다른 도구가 읽으면 깨진
JSON 을 봅니다. `crex compiledb` 는 임시 파일에 받아 성공했을 때만 제자리로
옮깁니다.
