# -*- coding: utf-8 -*-
"""
플라자CC 타이거 코스 오전 예약 가능 슬롯 조회 (API 직접 호출 버전)
- 로그인 → 비밀번호 변경 알림 건너뜀 → 회원예약 클릭 → 세션 확보
- 확보된 세션으로 6개 날짜 API 동시 조회 (빠름!)
- 오늘 기준 다음주/2주뒤/3주뒤 목·금
"""

import asyncio
import json
import re
import sys
from datetime import date, timedelta
from playwright.async_api import async_playwright, Page, Frame

# ── 계정 정보 ─────────────────────────────────
LOGIN_URL  = "https://www.plazacc.co.kr/plzcc/irsweb/golf2/member/login.do"
RESVE_URL  = "https://www.plazacc.co.kr/plzcc/irsweb/golf2/reservation/ircc_iqry_work_memb.do"
ID  = "sooholee@btstech.co.kr"
PW  = "9799Suho!@"

BOOKING_HOST   = "booking.hanwharesort.co.kr"
API_URL        = f"https://{BOOKING_HOST}/pzc/pmr/0010/doExecute.mvc"

TARGET_COURSE  = "타이거"
MORNING_HOURS  = set(range(6, 12))

# ── 날짜 계산 ─────────────────────────────────
def get_target_dates() -> list[date]:
    today = date.today()
    days_to_thu = (3 - today.weekday()) % 7 or 7
    next_thu = today + timedelta(days=days_to_thu)
    targets = []
    for w in range(3):
        thu = next_thu + timedelta(weeks=w)
        targets += [thu, thu + timedelta(days=1)]
    return sorted(set(targets))


# ── 프레임 탐색 헬퍼 ──────────────────────────
def find_frame(page: Page, substr: str) -> Frame | None:
    for f in page.frames:
        if substr in f.url:
            return f
    return None


async def wait_frame(page: Page, substr: str, timeout: float = 20) -> Frame | None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        f = find_frame(page, substr)
        if f:
            return f
        await asyncio.sleep(0.4)
    return None


# ── 로그인 + 예약 페이지 진입 ─────────────────
async def login_and_enter(page: Page):
    """
    1. 로그인
    2. 비밀번호 변경 알림 → 다음에 변경하기
    3. 메인 → 온라인예약 → 정기예약(회원예약) 클릭
    4. booking.hanwharesort.co.kr iframe 로드 대기
    """
    # 1) 로그인
    await page.goto(LOGIN_URL)
    await page.wait_for_load_state("networkidle")
    await page.fill('input[name="username"]', ID)
    await page.fill('input[name="userpwd"]', PW)
    await page.click('input#btnLogin')
    await page.wait_for_load_state("networkidle")
    print(f"[로그인] {page.url}")

    # 2) 비밀번호 변경 알림 건너뛰기
    if 'notify' in page.url or 'pw_' in page.url:
        await page.click('a[href="/plzcc/irsweb/golf2/main.do"]')
        await page.wait_for_load_state("networkidle")
        print(f"[로그인] 비번변경 알림 건너뜀 → {page.url}")

    # 3) 온라인예약 → 정기예약(회원예약)
    await page.click('a.reservationBtn')
    await asyncio.sleep(0.4)
    await page.click('a[href="#cont1"]')
    print("[예약진입] 정기예약 클릭")

    # 4) booking iframe 로드 대기
    booking = await wait_frame(page, BOOKING_HOST, timeout=20)
    if not booking:
        raise RuntimeError("booking.hanwharesort.co.kr iframe 로드 실패")
    print(f"[예약진입] booking iframe 확인: {booking.url[:60]}")

    # step1 로드 대기
    step1 = await wait_frame(page, "serviceF01", timeout=15)
    if not step1:
        raise RuntimeError("serviceF01 frame 로드 실패")

    # 용인 선택 + 확인
    await step1.click('#branchBtn1', force=True)
    await asyncio.sleep(1.5)
    await step1.click('a.confirmBtn', force=True)
    print("[예약진입] 용인 선택 완료")

    # step2(달력) 로드 대기
    await wait_frame(page, "serviceF02", timeout=15)
    print("[예약진입] 달력 로드 완료")


# ── 네트워크 요청 캡처로 API 파라미터 파악 ────
async def capture_api_params(page: Page) -> dict | None:
    """
    serviceF02에서 날짜 하나를 클릭해 네트워크 요청을 캡처,
    doExecute.mvc 의 INTF_ID 와 파라미터 구조를 파악한다.
    """
    captured = {}

    async def on_request(request):
        if "doExecute" in request.url and request.method == "POST":
            try:
                body = request.post_data or ""
                captured["url"]  = request.url
                captured["body"] = body
                captured["headers"] = dict(request.headers)
            except Exception:
                pass

    page.on("request", on_request)

    step2 = find_frame(page, "serviceF02")
    if not step2:
        return None

    # 첫 번째 클릭 가능한 날짜 클릭
    try:
        await step2.locator("td:not(.disabled) a").first.click(force=True)
        await asyncio.sleep(2)
    except Exception as e:
        print(f"[캡처] 날짜 클릭 실패: {e}")

    page.remove_listener("request", on_request)

    if captured:
        print(f"[캡처] API URL: {captured['url']}")
        print(f"[캡처] Body: {captured['body'][:200]}")
    return captured if captured else None


# ── 직접 API 호출로 슬롯 조회 ─────────────────
async def fetch_slots_api(page: Page, target: date, api_params: dict,
                           brch_cd: str, memb_no: str, cust_cl_cd: str) -> list[str]:
    """
    캡처된 파라미터를 바탕으로 doExecute.mvc 직접 호출
    """
    d8 = target.strftime("%Y%m%d")

    # 캡처된 body 파싱 후 날짜 교체
    body = api_params.get("body", "")
    # RSRV_DATE 교체
    if "RSRV_DATE" in body:
        body = re.sub(r'RSRV_DATE=[^&]*', f'RSRV_DATE={d8}', body)
    elif "playDate" in body:
        body = re.sub(r'playDate=[^&]*', f'playDate={d8}', body)

    headers = api_params.get("headers", {})
    # Content-Type 확인
    ct = headers.get("content-type", "application/x-www-form-urlencoded")

    try:
        response = await page.evaluate(f"""
            async () => {{
                const resp = await fetch("{api_params['url']}", {{
                    method: "POST",
                    headers: {{ "Content-Type": "{ct}" }},
                    body: decodeURIComponent("{body.replace('"', '\\"')}"),
                    credentials: "include"
                }});
                return await resp.text();
            }}
        """)
        return parse_slots_from_json(response, target)
    except Exception as e:
        print(f"  [API] {d8} 호출 실패: {e}")
        return []


def parse_slots_from_json(response_text: str, target: date) -> list[str]:
    results = []
    try:
        data = json.loads(response_text)
        # 응답 구조 탐색: ds.Data.ds_list 또는 유사 키
        items = []
        def find_lists(obj):
            if isinstance(obj, list):
                items.extend(obj)
            elif isinstance(obj, dict):
                for v in obj.values():
                    find_lists(v)
        find_lists(data)

        for item in items:
            if not isinstance(item, dict):
                continue
            # 코스명 확인
            cors_nm = str(item.get("CORS_NM", "") or item.get("cors_nm", "") or
                          item.get("COURSE_NM", "") or "")
            if TARGET_COURSE not in cors_nm:
                continue
            # 시간 확인
            time_str = str(item.get("RSRV_TIME", "") or item.get("tee_off_time", "") or
                           item.get("TIME", "") or "")
            tm = re.search(r'(\d{1,2}):?(\d{2})', time_str)
            if not tm:
                continue
            hour = int(tm.group(1))
            if hour not in MORNING_HOURS:
                continue
            # 예약 가능 여부
            avail = str(item.get("RSRV_POSBL_YN", "") or item.get("avail", "") or
                        item.get("AVAIL_YN", "") or "Y")
            if avail.upper() in ("N", "NO", "0"):
                continue
            t = f"{hour:02d}:{tm.group(2)}"
            results.append(f"{target.strftime('%Y-%m-%d(%a)')} {t} {TARGET_COURSE} ✅")
    except Exception:
        pass
    return results


# ── serviceF03 HTML 직접 파싱 (API 캡처 실패 시 폴백) ──
async def fetch_slots_html(page: Page, target: date) -> list[str]:
    """serviceF02 달력에서 날짜 클릭 → serviceF03 HTML 파싱"""
    d8  = target.strftime("%Y%m%d")
    day = str(target.day)

    step2 = find_frame(page, "serviceF02")
    if not step2:
        return []

    try:
        await step2.locator(f"td:not(.disabled) a:has-text('{day}')").first.click(force=True)
        await asyncio.sleep(2)
    except Exception:
        return []

    # 슬롯 프레임 찾기 (serviceF03 또는 날짜 클릭 후 로드되는 serviceS01)
    step3 = None
    for f in page.frames:
        if BOOKING_HOST in f.url and ("serviceS01" in f.url or "serviceF03" in f.url):
            step3 = f
            break

    if not step3:
        # serviceS01 도 체크 (날짜 클릭 후 실제 로드되는 프레임)
        for f in page.frames:
            if BOOKING_HOST in f.url and ("serviceS01" in f.url or "serviceF03" in f.url):
                step3 = f
                break

    if not step3:
        print(f"  [HTML] {target} 슬롯 프레임 미발견, 프레임 목록:")
        for f in page.frames:
            if BOOKING_HOST in f.url:
                print(f"    {f.url[:100]}")
        return []

    print(f"  [HTML] 슬롯 프레임: {step3.url[:80]}")
    html = await step3.content()
    with open(f"debug_step3_{d8}.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  [HTML] debug_step3_{d8}.html 저장")

    return parse_slots_from_html(html, target)


def parse_slots_from_html(html: str, target: date) -> list[str]:
    """
    #yongin 테이블 구조:
    각 행 = 4쌍의 td (시간, 상태):
      0=타이거OUT  1=타이거IN  2=라이온OUT  3=라이온IN
    상태: '예약' = 가능, '마감' = 불가
    """
    results = []
    # yongin 테이블 추출
    idx = html.find('id="yongin"')
    if idx < 0:
        idx = html.find("yongin")
    if idx < 0:
        return results

    chunk = html[idx:idx + 40000]

    # 모든 td 내용 추출
    all_tds = re.findall(r'<td[^>]*>(.*?)</td>', chunk, re.DOTALL)
    td_texts = []
    for td in all_tds:
        text = re.sub(r'<[^>]+>', '', td).strip()
        td_texts.append(text)

    # 4쌍씩 처리: 0=타이거OUT, 1=타이거IN, 2=라이온OUT, 3=라이온IN
    col_names = ["타이거(OUT)", "타이거(IN)", "라이온(OUT)", "라이온(IN)"]
    i = 0
    while i + 7 < len(td_texts):
        # 8개 td = 4쌍 (시간, 상태)
        time0 = td_texts[i]
        # 첫 td가 시간 형식인지 확인
        if not re.match(r'^\d{1,2}:\d{2}$', time0):
            i += 1
            continue
        # 타이거 코스만 확인 (col 0, 1)
        for col in range(2):
            time_str = td_texts[i + col * 2]
            status   = td_texts[i + col * 2 + 1]
            tm = re.match(r'(\d{1,2}):(\d{2})', time_str)
            if not tm:
                continue
            hour = int(tm.group(1))
            if hour not in MORNING_HOURS:
                continue
            if '예약' in status and '마감' not in status:
                t = f"{hour:02d}:{tm.group(2)}"
                results.append(
                    f"{target.strftime('%Y-%m-%d(%a)')} {t} {col_names[col]} ✅"
                )
        i += 8  # 다음 행 (4쌍 = 8 td)
    return results


# ── 메인 ──────────────────────────────────────
async def main():
    targets = get_target_dates()
    print(f"조회 날짜: {[d.strftime('%Y-%m-%d(%a)') for d in targets]}\n")

    all_slots: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=200)
        page = await browser.new_page()

        # 로그인 + 예약 페이지 진입
        try:
            await login_and_enter(page)
        except Exception as e:
            print(f"[오류] 예약 페이지 진입 실패: {e}")
            input("Enter 로 종료...")
            await browser.close()
            return

        # 네트워크 요청 캡처로 API 파라미터 파악
        print("\n[API] 네트워크 요청 캡처 중 (달력에서 날짜 1회 클릭)...")
        api_params = await capture_api_params(page)

        if api_params:
            # ── 빠른 경로: API 직접 호출 ──────────────
            print(f"\n[API] {len(targets)}개 날짜 병렬 조회 시작...\n")
            tasks = [
                fetch_slots_api(page, t, api_params, "0400", "", "")
                for t in targets
            ]
            results = await asyncio.gather(*tasks)
            for t, slots in zip(targets, results):
                label = t.strftime('%Y-%m-%d(%a)')
                if slots:
                    for s in slots:
                        print(f"  {s}")
                    all_slots.extend(slots)
                else:
                    print(f"  {label} — 없음")
        else:
            # ── 폴백: HTML 파싱 ────────────────────
            print("\n[폴백] HTML 방식으로 날짜별 순차 조회...\n")
            for target in targets:
                print(f"── {target.strftime('%Y-%m-%d (%A)')} 조회 중...")
                slots = await fetch_slots_html(page, target)
                if slots:
                    for s in slots:
                        print(f"  {s}")
                    all_slots.extend(slots)
                else:
                    print(f"  없음")

        print("\n" + "=" * 55)
        print("【 타이거 코스 오전 예약 가능 슬롯 】")
        if all_slots:
            for s in all_slots:
                print(f"  {s}")
        else:
            print("  없음")
        print("=" * 55)

        await browser.close()


if __name__ == "__main__":
    # Windows 콘솔 UTF-8 출력
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    asyncio.run(main())
