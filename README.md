# myAITrend
AI트렌드 수집

## 수집기 실행

```bash
pip install -r requirements.txt
python -m backend.collector          # 1회 수집
uvicorn backend.main:app --port 8000 # API 서버 (10분 주기 수집 포함)
```

서버를 띄운 뒤 `http://localhost:8000/` 에서 수집 결과를 볼 수 있습니다 (API 문서는 `/docs`).

## 자동 수집 (GitHub Actions)

`.github/workflows/collect.yml` 이 30분마다 수집기를 실행하고, `data/daily/` 에 변경이 있을 때만 커밋·푸시합니다.
수동 실행은 Actions 탭의 `collect` → `Run workflow` 입니다.

### GitHub Secrets

현재 수집 소스(GeekNews와 AWS·Google·네이버·카카오·LY·뱅크샐러드·토스·당근·무신사의 한국어 기술 블로그)는
인증키가 필요 없어서 **등록할 Secret이 없습니다.**

나중에 인증이 필요한 소스를 추가하면 키 값은 코드에 쓰지 말고, 저장소 Settings → Secrets and variables → Actions 에 아래 형식으로 등록하고 워크플로에서 `${{ secrets.<이름> }}` 로 참조합니다.

| Secret 이름 (예시) | 용도 |
|---|---|
| `<SOURCE_NAME>_API_KEY` | 해당 소스 인증키 (추가 시 여기에 기록) |

워크플로 권한은 `contents: write` 하나만 쓰며, 기본 `GITHUB_TOKEN` 으로 커밋합니다.
