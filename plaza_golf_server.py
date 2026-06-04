# -*- coding: utf-8 -*-
"""
플라자CC 타이거 코스 오전 예약 가능 슬롯 조회 — GitHub Actions 서버용
- headless 모드로 실행
- 결과를 Telegram 으로 전송
- 환경변수: PLAZACC_ID, PLAZACC_PW, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import asyncio
import os
import re
import sys
from datetime import date, timedelta

import httpx
from playwright.async_api import async_playwright, Frame, Page

ID = os.environ.get("PLAZACC_ID", "sooholee@btstech.co.kr")
PW = os.environ.get("PLAZACC_PW", "9799Suho!@")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

LOGIN_URL    = "https://www.plazacc.co.kr/plzcc/irsweb/golf2/member/login.do"
BOOKING_HOST = "booking.hanwharesort.co.kr"
MORNING_HOURS = set(range(6, 12))
COL_NAMES     = ["타이거(OUT)", "타이거(IN)", "라이온(OUT)", "라이온(IN)"]


def get_date_range() -> list[date]:
    today = date.today()
    return [today + timedelta(days=i) for i in range(1, 31)]


async def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] 토큰 없음 — 콘솔 출력만")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        })


async def wait_frame(page: Page, substr: str, timeout: float = 20) -> Frame | None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for f in page.frames:
            if substr in f.url:
                return f
        await asyncio.sleep(0.4)
    return None


async def login_and_enter(page: Page):
    await page.goto(LOGIN_URL)
    await page.wait_for_load_state("networkidle")
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


async def main():
    global _month_advances
    _month_advances = 0

    dates = get_date_range()
    print(f"조회 시작: {dates[0]} ~ {dates[-1]}")
    await send_telegram(f"🏌️ 플라자CC 타이거 코스 조회 시작\n{dates[0]} ~ {dates[-1]}")

    all_slots: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()

        try:
            await login_and_enter(page)
        except Exception as e:
            msg = f"❌ 로그인/진입 실패: {e}"
            print(msg)
            await send_telegram(msg)
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

    # ── 결과 전송 ──────────────────────────────
    today_str = date.today().strftime("%Y-%m-%d")
    if all_slots:
        lines = [f"🏌️ <b>타이거 코스 오전 예약 가능</b> ({today_str} 기준)\n"]
        prev_date = ""
        for s in all_slots:
            d = s[:10]
            if d != prev_date:
                lines.append(f"\n📅 <b>{d}</b>")
                prev_date = d
            time_part = s[12:17]
            course    = s[18:]
            lines.append(f"  ⏰ {time_part}  {course}")
        lines.append(f"\n총 {len(all_slots)}개 슬롯")
        msg = "\n".join(lines)
    else:
        msg = f"🏌️ 타이거 코스 오전 예약 가능 슬롯 없음 ({today_str} 기준)"

    print("\n" + msg)
    await send_telegram(msg)


if __name__ == "__main__":
    asyncio.run(main())
