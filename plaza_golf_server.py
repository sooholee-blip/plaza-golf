# -*- coding: utf-8 -*-
"""
플라자CC 타이거 코스 오전 예약 가능 슬롯 조회 — GitHub Actions 서버용
- headless 모드로 실행
- 결과를 Gmail 로 전송
- 환경변수: PLAZACC_ID, PLAZACC_PW, GMAIL_USER, GMAIL_APP_PW, NOTIFY_EMAIL
"""

import asyncio
import os
import re
import smtplib
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.async_api import async_playwright, Frame, Page

# ── 환경변수 ──────────────────────────────────
ID           = os.environ.get("PLAZACC_ID", "sooholee@btstech.co.kr")
PW           = os.environ.get("PLAZACC_PW", "9799Suho!@")
GMAIL_USER   = os.environ.get("GMAIL_USER", "")       # 발송 Gmail 주소
GMAIL_APP_PW = os.environ.get("GMAIL_APP_PW", "")     # Gmail 앱 비밀번호
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "")     # 수신 이메일 (핸드폰 연동)

LOGIN_URL    = "https://www.plazacc.co.kr/plzcc/irsweb/golf2/member/login.do"
BOOKING_HOST = "booking.hanwharesort.co.kr"
MORNING_HOURS = set(range(6, 12))
COL_NAMES     = ["타이거(OUT)", "타이거(IN)", "라이온(OUT)", "라이온(IN)"]


# ── 이메일 발송 ───────────────────────────────
def send_email(subject: str, body_html: str):
    if not GMAIL_USER or not GMAIL_APP_PW or not NOTIFY_EMAIL:
        print("[이메일] 설정 없음 — 콘솔 출력만")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PW)
        server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())

    print(f"[이메일] 발송 완료 → {NOTIFY_EMAIL}")


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
    await page.goto(LOGIN_URL, timeout=60000)
    await page.wait_for_load_state("networkidle", timeout=60000)
    await page.fill('input[name="username"]', ID)
    await page.fill('input[name="userpwd"]', PW)
    await page.click('input#btnLogin')
    await page.wait_for_load_state("networkidle")

    if "notify" in page.url or "pw_" in page.url:
        await page.click('a[href="/plzcc/irsweb/golf2/main.do"]')
        await page.wait_for_load_state("networkidle")

    await page.click("a.reservationBtn")
    await asyncio.sleep(0.4)
    await page.click('a[href="#cont1"]')
    await wait_frame(page, BOOKING_HOST, timeout=20)

    step1 = await wait_frame(page, "serviceF01", timeout=15)
    if not step1:
        raise RuntimeError("serviceF01 로드 실패")
    await step1.click("#branchBtn1", force=True)
    await asyncio.sleep(1.5)
    await step1.click("a.confirmBtn", force=True)

    if not await wait_frame(page, "serviceF02", timeout=15):
        raise RuntimeError("serviceF02 로드 실패")


# ── 달력 유틸 ─────────────────────────────────
async def get_calendar_dates(step2: Frame) -> set[str]:
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
    cal = await get_calendar_dates(step2)
    parsed = []
    for d8 in cal:
        try:
            parsed.append(date(int(d8[:4]), int(d8[4:6]), int(d8[6:])))
        except Exception:
            pass
    return max(parsed) if parsed else None


async def goto_next_month(step2: Frame) -> bool:
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
_month_advances: int = 0


async def select_date(page: Page, target: date) -> bool:
    global _month_advances
    step2 = await wait_frame(page, "serviceF02", timeout=10)
    if not step2:
        return False

    d8 = target.strftime("%Y%m%d")

    while _month_advances < 2:
        max_d = await calendar_max_date(step2)
        if max_d is None or target <= max_d:
            break
        if not await goto_next_month(step2):
            return False
        _month_advances += 1

    if d8 not in await get_calendar_dates(step2):
        return False

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


# ── 슬롯 읽기 + 파싱 ──────────────────────────
async def read_slots(page: Page, target: date) -> list[str]:
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
        for col in range(2):
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
                    f"{target.strftime('%Y-%m-%d(%a)')} {t} {COL_NAMES[col]}"
                )
        i += 8
    return results


# ── 결과 → HTML 이메일 본문 ───────────────────
def build_email(slots: list[str], query_date: str) -> tuple[str, str]:
    if not slots:
        subject = f"[플라자CC] 타이거 코스 오전 예약 가능 슬롯 없음 ({query_date})"
        body    = f"<p>조회일: {query_date}<br>예약 가능한 타이거 코스 오전 슬롯이 없습니다.</p>"
        return subject, body

    subject = f"[플라자CC] 타이거 코스 오전 {len(slots)}개 슬롯 가능 ({query_date})"

    rows = ""
    prev_date = ""
    for s in slots:
        d        = s[:10]                    # 2026-06-08
        dow      = s[11:14]                   # Mon
        time_p   = s[16:21]                   # 06:02
        course   = s[22:]                     # 타이거(OUT)
        if d != prev_date:
            rows += f'<tr><td colspan="2" style="background:#1a5c38;color:white;padding:8px 12px;font-weight:bold;">📅 {d} ({dow})</td></tr>'
            prev_date = d
        rows += (
            f'<tr>'
            f'<td style="padding:6px 20px;">⏰ {time_p}</td>'
            f'<td style="padding:6px 20px;color:#1a5c38;font-weight:bold;">{course}</td>'
            f'</tr>'
        )

    body = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:auto;">
      <h2 style="background:#1a5c38;color:white;padding:16px;border-radius:8px 8px 0 0;margin:0;">
        🏌️ 플라자CC 타이거 코스
      </h2>
      <p style="color:#666;margin:12px 0 4px;">조회일: {query_date} &nbsp;|&nbsp; 총 {len(slots)}개 슬롯</p>
      <table style="width:100%;border-collapse:collapse;border:1px solid #ddd;border-radius:0 0 8px 8px;overflow:hidden;">
        {rows}
      </table>
      <p style="color:#999;font-size:12px;margin-top:12px;">
        * 오전(06:00~11:59) 예약 가능 시간대만 표시됩니다.
      </p>
    </div>
    """
    return subject, body


# ── 메인 ──────────────────────────────────────
async def main():
    global _month_advances
    _month_advances = 0

    dates     = get_date_range()
    today_str = date.today().strftime("%Y-%m-%d")
    print(f"조회 시작: {dates[0]} ~ {dates[-1]}")

    all_slots: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page    = await browser.new_page()

        try:
            await login_and_enter(page)
        except Exception as e:
            print(f"[오류] 로그인/진입 실패: {e}")
            send_email(
                f"[플라자CC] 조회 실패 ({today_str})",
                f"<p>오류: {e}</p>"
            )
            await browser.close()
            return

        for target in dates:
            if not await select_date(page, target):
                continue
            slots = await read_slots(page, target)
            all_slots.extend(slots)
            for s in slots:
                print(f"  ✅ {s}")

        await browser.close()

    # ── 이메일 발송 ────────────────────────────
    subject, body = build_email(all_slots, today_str)
    print(f"\n결과: {len(all_slots)}개 슬롯 발견")
    send_email(subject, body)


if __name__ == "__main__":
    asyncio.run(main())
