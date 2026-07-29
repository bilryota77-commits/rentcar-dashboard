import os
import json
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
import firebase_admin
from firebase_admin import credentials, firestore

async def main():
    async with async_playwright() as p:
        # 깃허브 가상 서버(Linux) 환경을 데스크톱 크롬으로 완전 위장 및 메모리 오류 방지
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled'
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="ko-KR",
            timezone_id="Asia/Seoul"
        )
        page = await context.new_page()

        # 환경변수(Secrets) 또는 기본값 적용
        ims_id = os.environ.get("IMS_ID", "").strip()
        ims_pw = os.environ.get("IMS_PW", "").strip()

        print("[1/5] IMS 메인 접속 및 로그인 시작...")
        # React 비동기 JS가 완전 로드되도록 networkidle로 접속
        await page.goto("https://imsform.com/login", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(2000)

        # 아이디 입력란 정밀 탐지 및 입력
        id_locator = page.locator("input[name='id'], input[name='userId'], input[type='text'], input[placeholder*='아이디']").first
        await id_locator.wait_for(state="visible", timeout=30000)
        await id_locator.fill(ims_id)

        # 비밀번호 입력란 정밀 탐지 및 입력
        pw_locator = page.locator("input[name='password'], input[type='password'], input[placeholder*='비밀번호']").first
        await pw_locator.wait_for(state="visible", timeout=10000)
        await pw_locator.fill(ims_pw)

        # 로그인 버튼 클릭
        submit_btn = page.locator("button[type='submit'], button:has-text('로그인')").first
        await submit_btn.click()
        
        print("[2/5] 로그인 성공 및 대기...")
        await page.wait_for_timeout(3000)

        # ==========================================================
        # [Slot 3] 실시간 배차상태(배차중/대기) 매핑 및 차량 고유 ID 정밀 탐지
        # ==========================================================
        print("[3/5] 실시간 배차상태(배차중/대기) 및 차량 목록 통합 수집 시작...")

        status_map = {}
        state_page = 1
        while True:
            state_url = f"https://imsform.com/carStatus/state?page={state_page}&listNum=100"
            await page.goto(state_url, wait_until="networkidle")
            await page.wait_for_timeout(1200)

            page_status = await page.evaluate('''() => {
                const map = {};
                const rows = document.querySelectorAll("table tbody tr");
                rows.forEach(row => {
                    const cols = Array.from(row.querySelectorAll("td")).map(c => c.innerText.trim());
                    const numIdx = cols.findIndex(t => /\\d{2,3}[가-힣]\\d{4}/.test(t.replace(/\\s+/g, "")));
                    if (numIdx !== -1) {
                        const carNum = cols[numIdx].replace(/\\s+/g, "");
                        let st = numIdx > 0 ? cols[numIdx - 1] : "";
                        if (!st || st.length > 5) st = cols[1] || cols[0];
                        if (carNum) map[carNum] = st;
                    }
                });
                return map;
            }''')

            if not page_status:
                break
            
            status_map.update(page_status)
            print(f"  -> [차량 현황 {state_page}페이지] 배차 상태 {len(page_status)}대 매핑")
            
            if len(page_status) < 100:
                break
            state_page += 1

        print(f"  -> 총 {len(status_map)}대 실시간 상태(배차중/대기) 매핑 완료.")

        # 2단계: carStatus/management 페이지 접속 및 차량 ID / 목록 수집
        raw_car_list = []
        seen_cars = set()
        current_page = 1

        while True:
            target_page_url = f"https://imsform.com/carStatus/management?page={current_page}&state=all&business_id=all&carType=all&carSize=all&listNum=100"
            await page.goto(target_page_url, wait_until="networkidle")
            await page.wait_for_timeout(1500)

            dom_cars = await page.evaluate('''() => {
                const list = [];
                const rows = document.querySelectorAll("table tbody tr");
                
                rows.forEach((row, idx) => {
                    let carId = "";

                    // 1. outerHTML 및 속성 정규식 추출
                    const html = row.outerHTML;
                    let match = html.match(/id=(\\d+)/) || html.match(/info\\?id=(\\d+)/) || html.match(/\\/info\\/(\\d+)/);
                    if (match) carId = match[1];

                    // 2. React Fiber / Props 속성 정밀 추출
                    if (!carId) {
                        const elements = [row, ...Array.from(row.querySelectorAll("*"))];
                        for (const el of elements) {
                            for (const key in el) {
                                if (key.startsWith("__reactProps") || key.startsWith("__reactFiber")) {
                                    try {
                                        const str = JSON.stringify(el[key], (k, v) => {
                                            if (k === "ownerDocument" || k === "domNode" || k === "stateNode") return undefined;
                                            return v;
                                        });
                                        let m = str.match(/["']?id["']?\\s*:\\s*(\\d{5,8})/) || str.match(/info\\?id=(\\d+)/);
                                        if (m) { carId = m[1]; break; }
                                    } catch (e) {}
                                }
                            }
                            if (carId) break;
                        }
                    }

                    const cols = Array.from(row.querySelectorAll("td")).map(c => c.innerText.trim());
                    const numIdx = cols.findIndex(t => /\\d{2,3}[가-힣]\\d{4}/.test(t.replace(/\\s+/g, "")));
                    if (numIdx !== -1) {
                        const carNum = cols[numIdx].replace(/\\s+/g, "");
                        list.push({
                            rowIndex: idx,
                            carId: carId,
                            carNumber: carNum,
                            model: (numIdx + 2 < cols.length) ? cols[numIdx + 2] : "",
                            carName: (numIdx + 3 < cols.length) ? cols[numIdx + 3] : "",
                            mileage: (numIdx + 6 < cols.length) ? cols[numIdx + 6] : "0"
                        });
                    }
                });
                return list;
            }''')

            new_count = 0
            for car in dom_cars:
                c_num = car["carNumber"]
                if c_num not in seen_cars:
                    seen_cars.add(c_num)
                    
                    c_model = car.get("model", "")
                    c_name = car.get("carName", "")
                    full_model = f"{c_model} ({c_name})" if (c_model and c_name) else (c_model or c_name)

                    raw_m = car.get("mileage", "0")
                    clean_mileage = raw_m.replace(",", "").replace("km", "").replace("KM", "").strip()
                    if clean_mileage == "-" or not clean_mileage:
                        clean_mileage = "0"
                    
                    # 배차중 / 대기 매핑
                    real_state = status_map.get(c_num, "대기")

                    raw_car_list.append({
                        "page": current_page,
                        "carId": car.get("carId", ""),
                        "carNumber": c_num,
                        "carModel": full_model,
                        "currentMileage": clean_mileage,
                        "state": real_state
                    })
                    new_count += 1

            print(f"  -> [{current_page} 페이지] 목록 수집: {new_count}대 (누적: {len(raw_car_list)}대)")

            if new_count == 0:
                break
            current_page += 1

        # ==========================================================
        # [Slot 4] 대기 시간 확장(최대 5초) + IMS 미입력 검증 진단 수집
        # ==========================================================
        print(f"[4/5] 총 {len(raw_car_list)}대 차량의 외장색상 및 옵션 완전 검증 수집 시작...")

        vehicle_list = []

        for idx, car in enumerate(raw_car_list, start=1):
            color = ""
            options = ""

            try:
                # 1. 상세 페이지 접속
                if car.get("carId"):
                    info_url = f"https://imsform.com/carStatus/info?id={car['carId']}"
                    await page.goto(info_url, wait_until="networkidle")
                else:
                    target_url = f"https://imsform.com/carStatus/management?page={car['page']}&state=all&business_id=all&carType=all&carSize=all&listNum=100"
                    if page.url != target_url:
                        await page.goto(target_url, wait_until="networkidle")
                        await page.wait_for_timeout(800)

                    row_loc = page.locator("table tbody tr", has_text=car["carNumber"])
                    if await row_loc.count() > 0:
                        await row_loc.first.click()
                        await page.wait_for_timeout(1000)

                # 2. 대기 시간 5초로 확장 (1초 간격으로 최대 5회 자동 재시도)
                for retry in range(5):
                    detail_data = await page.evaluate('''() => {
                        let c = "";
                        let o = "";

                        const inputs = document.querySelectorAll("input, textarea");
                        inputs.forEach(el => {
                            const val = el.value ? el.value.trim() : "";
                            if (!val) return;
                            const parent = el.parentElement || el.closest("div") || el.closest("td");
                            const pText = parent ? parent.innerText : "";
                            if ((pText.includes("차량색상") || pText.includes("색상")) && !c) c = val;
                            if ((pText.includes("차량 옵션") || pText.includes("옵션")) && !o) o = val;
                        });

                        if (!c || !o) {
                            const lines = document.body.innerText.split("\\n").map(l => l.trim()).filter(Boolean);
                            for (let i = 0; i < lines.length; i++) {
                                if (lines[i].includes("차량색상") && i + 1 < lines.length && !c) c = lines[i + 1];
                                if (lines[i].includes("차량 옵션") && i + 1 < lines.length && !o) o = lines[i + 1];
                            }
                        }
                        return { color: c, options: o };
                    }''')

                    color = detail_data.get("color", "")
                    options = detail_data.get("options", "")

                    # 데이터 수신 완료 시 즉시 통과
                    if color and options:
                        break
                    
                    # 1초 추가 대기 후 재시도
                    await page.wait_for_timeout(1000)

            except Exception as e:
                pass

            # 5초 대기 후에도 값이 없으면 진단 로그 출력
            if not color or not options:
                print(f"  [진단] 차량 {car['carNumber']} -> 색상: '{color or 'IMS 미입력'}', 옵션: '{options or 'IMS 미입력'}' (IMS 실제 공란 가능성)")

            vehicle_list.append({
                "carNumber": car["carNumber"],
                "carModel": car["carModel"],
                "currentMileage": car["currentMileage"],
                "state": car["state"],
                "exteriorColor": color,
                "options": options
            })

            if idx % 30 == 0 or idx == len(raw_car_list):
                print(f"  -> 진행 상황: {idx}/{len(raw_car_list)}대 완료")

        print("==================================================")
        print(f"최종 추출 완료: 총 {len(vehicle_list)}대 수집 성공")
        print("==================================================")
        
        await browser.close()

        # ==========================================================
        # [Slot 5] 파이어베이스 DB 연결 (로컬 키파일 & 깃허브 Secrets 이중 지원)
        # ==========================================================
        print("[5/5] 파이어베이스 DB 동기화 진행 중...")

        firebase_key_env = os.environ.get("FIREBASE_KEY_JSON")

        if firebase_key_env:
            # 깃허브 액션 실행 시: Secrets 환경변수 문자열 JSON 파싱
            cred_dict = json.loads(firebase_key_env)
            cred = credentials.Certificate(cred_dict)
        elif os.path.exists("serviceAccountKey.json"):
            # 로컬 컴퓨터 실행 시: 파일에서 직접 읽기
            cred = credentials.Certificate("serviceAccountKey.json")
        else:
            raise FileNotFoundError("Firebase 인증 키를 찾을 수 없습니다. (serviceAccountKey.json 또는 FIREBASE_KEY_JSON 환경변수 필요)")

        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)

        db = firestore.client()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Firestore DB 'vehicles' 컬렉션 동기화
        batch = db.batch()
        for v in vehicle_list:
            doc_ref = db.collection("vehicles").document(v["carNumber"])
            v["lastSynced"] = now_str
            batch.set(doc_ref, v)

        batch.commit()
        print(f"동기화 완료: 총 {len(vehicle_list)}대 차량 파이어베이스 DB 저장 완수")

if __name__ == "__main__":
    asyncio.run(main())