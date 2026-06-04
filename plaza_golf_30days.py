# -*- coding: utf-8 -*-
"""
플라자CC 타이거 코스 오전 예약 가능 슬롯 조회 — 30일 전체 스캔

실행: python plaza_golf_30days.py
- 오늘 기준 내일~30일 이내 모든 날짜 조회
- 타이거 코스(OUT/IN) 오전(06:00~11:59) 예약 가능 시간대 출력
"""

import asyncio
import io
import re
import sys
from datetime import date, timedelta

from playwright.async_api import async_playwright, Frame, Page

# UTF-8 출력 (Windows 터미널 대응)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── 설정 ──────────────────────────────────────
LOGIN_URL    = "https://www.plazacc.co.kr/plzcc/irsweb/golf2/member/login.do"
BOOKING_HOST = "booking.hanwharesort.co.kr"
ID = "sooholee@btstech.co.kr"
PW = "9799Suho!@"

MORNING_HOURS = set(range(6, 12))   # 06:00 ~ 11:59
COL_NAMES     = ["타이거(OUT)", "타이거(IN)", "라이온(OUT)", "라이온(IN)"]


# ── 날짜 범위 ─────────────────────────────────
def get_date_range() -> list[date]:
    today = date.today()
    return [today + timedelta(days=i) for i in range(1, 31)]


# ── 프레임 대기 ───────────────────────────────
async def wait_frame(page: Page, substr: str, timeout: float = 20) -> Frame | None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for f in page.frames:
            if substr in f.url:
                return f
        await asyncio.sleep(0.4)
    return None


# ── 로그인 + 예약 페이지 진입 ─────────────────
async def login_and_enter(page: Page):
    await page.goto(LOGIN_URL)
    await page.wait_for_load_state("networkidle")
    await page.fill('input[name="username"]', ID)
    await page.fill('input[name="userpwd"]', PW)
    await page.click('input#btnLogin')
    await page.wait_for_load_state("networkidle")

    # 비밀번호 변경 알림 → 다음에 변경하기
    if "notify" in page.url or "pw_" in page.url:
        await page.click('a[href="/plzcc/irsweb/golf2/main.do"]')
        await page.wait_for_load_state("networkidle")

    # 온라인예약 → 정기예약(회원예약)
    await page.click("a.reservationBtn")
    await asyncio.sleep(0.4)
    await page.click('a[href="#cont1"]')

    # booking.hanwharesort.co.kr iframe 로드 대기
    await wait_frame(page, BOOKING_HOST, timeout=20)

    # Step1: 용인 선택 → 확인
    step1 = await wait_frame(page, "serviceF01", timeout=15)
    if not step1:
        raise RuntimeError("serviceF01 로드 실패")
    await step1.click("#branchBtn1", force=True)
    await asyncio.sleep(1.5)
    await step1.click("a.confirmBtn", force=True)

    # Step2: 달력 로드 대기
    if not await wait_frame(page, "serviceF02", timeout=15):
        raise RuntimeError("serviceF02 로드 실패")


# ── 달력 유틸 ─────────────────────────────────
async def get_calendar_dates(step2: Frame) -> set[str]:
    """현재 달력에서 checkReserveRule 을 가진 모든 날짜(YYYYMMDD) 반환"""
    try:
        result = await step2.evaluate("""
            () => {
                const dates = [];
                document.querySelectorAll('a[onclick]').forEach(a => {
                    const m = (a.getAttribute('onclick') || '').match(/(202\\d{5})/);
                    if (m) dates.push(m[1]);
                });
                return [...new Set(dates)];
            }
        """)
        return set(result)
    except Exception:
        return set()


async def calendar_max_date(step2: Frame) -> date | None:
    """현재 달력에 표시된 가장 마지막 날짜"""
    cal = await get_calendar_dates(step2)
    parsed = []
    for d8 in cal:
        try:
            parsed.append(date(int(d8[:4]), int(d8[4:6]), int(d8[6:])))
        except Exception:
            pass
    return max(parsed) if parsed else None


async def goto_next_month(step2: Frame) -> bool:
    """달력 다음달 버튼 클릭 (JS 직접 호출)"""
    try:
        ok = await step2.evaluate("""
            () => {
                const btn = document.getElementById('nextCalBtn')
                         || document.querySelector('a.nextMonth');
                if (btn) { btn.click(); return true; }
                return false;
            }
        """)
        if ok:
            await asyncio.sleep(1.5)
        return bool(ok)
    except Exception:
        return False


# ── 날짜 선택 ─────────────────────────────────
_month_advances: int = 0   # 다음달 이동 횟수


async def select_date(page: Page, target: date) -> bool:
    """
    달력에서 target 날짜 클릭.
    - 현재 달력 범위를 초과하면 다음달로 이동 (최대 2회)
    - 달력에 날짜가 없으면(마감·비활성) False 반환
    """
    global _month_advances

    step2 = await wait_frame(page, "serviceF02", timeout=10)
    if not step2:
        return False

    d8 = target.strftime("%Y%m%d")

    # 필요 시 다음달 이동 (최대 2회)
    while _month_advances < 2:
        max_d = await calendar_max_date(step2)
        if max_d is None or target <= max_d:
            break
        if not await goto_next_month(step2):
            return False
        _month_advances += 1

    # 날짜가 달력에 존재하는지 확인
    if d8 not in await get_calendar_dates(step2):
        return False  # 예약 불가(마감/비활성) 날짜

    # JS 로 해당 날짜 링크 클릭
    try:
        await step2.evaluate(f"""
            () => {{
                for (const a of document.querySelectorAll('a[onclick]')) {{
                    if ((a.getAttribute('onclick') || '').includes('{d8}')) {{
                        a.click(); return;
                    }}
                }}
            }}
        """)
        await asyncio.sleep(1.5)
        return True
    except Exception:
        return False


# ── 슬롯 읽기 ─────────────────────────────────
async def read_slots(page: Page, target: date) -> list[str]:
    """serviceS01 iframe HTML 에서 타이거 코스 오전 슬롯 파싱"""
    step3 = None
    for _ in range(20):
        for f in page.frames:
            if BOOKING_HOST in f.url and "serviceS01" in f.url:
                step3 = f
                break
        if step3:
            break
        await asyncio.sleep(0.3)

    if not step3:
        return []

    return parse_slots(await step3.content(), target)


def parse_slots(html: str, target: date) -> list[str]:
    """
    #yongin 테이블 구조: 행마다 td 8개 (4쌍)
      0=타이거(OUT)  1=타이거(IN)  2=라이온(OUT)  3=라이온(IN)
    상태: '예약' = 가능  /  '마감' = 불가
    """
    results = []
    idx = html.find('id="yongin"')
    if idx < 0:
        return results

    all_tds = re.findall(r"<td[^>]*>(.*?)</td>", html[idx:idx + 50000], re.DOTALL)
    texts   = [re.sub(r"<[^>]+>", "", td).strip() for td in all_tds]

    i = 0
    while i + 7 < len(texts):
        if not re.match(r"^\d{1,2}:\d{2}$", texts[i]):
            i += 1
            continue
        for col in range(2):           # 타이거 OUT(0), IN(1) 만 확인
            time_str = texts[i + col * 2]
            status   = texts[i + col * 2 + 1]
            m = re.match(r"(\d{1,2}):(\d{2})", time_str)
            if not m:
                continue
            hour = int(m.group(1))
            if hour not in MORNING_HOURS:
                continue
            if "예약" in status and "마감" not in status:
                t = f"{hour:02d}:{m.group(2)}"
                results.append(
                    f"{target.strftime('%Y-%m-%d(%a)')} {t} {COL_NAMES[col]} ✅"
                )
        i += 8
    return results


# ── 메인 ──────────────────────────────────────
async def main():
    global _month_advances
    _month_advances = 0

    dates = get_date_range()
    print(f"조회 범위: {dates[0]} ~ {dates[-1]} ({len(dates)}일)\n")

    all_slots: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=150)
        page    = await browser.new_page()

        print("로그인 및 예약 페이지 진입 중...")
        await login_and_enter(page)
        print("준비 완료. 날짜 조회 시작...\n")

        for target in dates:
            label = target.strftime("%Y-%m-%d(%a)")
            if not await select_date(page, target):
                continue                        # 마감/비활성 날짜는 조용히 건너뜀

            slots = await read_slots(page, target)
            if slots:
                for s in slots:
                    print(f"  {s}")
                all_slots.extend(slots)

        # ── 최종 요약 ──────────────────────────
        print("\n" + "=" * 62)
        print("【 타이거 코스 오전 예약 가능 슬롯 — 내일~30일 전체 】")
        print("=" * 62)
        if all_slots:
            prev_month = ""
            for s in all_slots:
                month = s[5:7]
                if month != prev_month and prev_month:
                    print()
                print(f"  {s}")
                prev_month = month
        else:
            print("  없음")
        print("=" * 62)
        print(f"총 {len(all_slots)}개 슬롯 발견")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
