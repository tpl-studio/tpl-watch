# TPL watch

공개 RSS(Google 뉴스 · Sky Sports · BBC · Guardian)를 5분마다(24시간) 확인합니다.
빅클럽 16곳의 확정 소식(로마노 "Here we go" · 로마노 경질·사임 보도 · 구단 공식 발표)으로 보이는 새 기사가 보이면
비공개 제작 워크플로를 바로 실행시킵니다. 사실 확인 · 중복 방지 · 카드 제작 · 발송은 모두 비공개 쪽에서 합니다.

- `watch.py` — 감시기(표준 라이브러리만)
- `rules.py` · `watch.toml` — 감지 규칙·피드 목록(자동 생성, 직접 고치지 않음)
- `.github/workflows/watch.yml` — 5분 예약

호출 전에 비공개 저장소의 이번 달 실행 기록으로 사용 시간을 계산해, 무료 한도(2,000분)를 넘길 것 같으면 호출하지 않습니다.

이 저장소에는 비밀값이 없습니다. 호출용 토큰과 대상 저장소 이름은 GitHub Secrets(`TPL_DISPATCH_TOKEN`, `TPL_DISPATCH_REPO`)에만 있습니다.
