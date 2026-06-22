import os
import requests
import hashlib
import hmac
import base64
import urllib.request
import urllib.parse
import concurrent.futures
import re
import json
import time         
import datetime
import streamlit as st
import pandas as pd 
import firebase_admin
from firebase_admin import credentials, firestore
import altair as alt

st.set_page_config(layout="wide")

# ==========================================
# [SLOT 0] 필수 설정 및 API 연동 키 구역
# ==========================================
NAVER_API_KEY = st.secrets.get("NAVER_API_KEY", "")
NAVER_SECRET_KEY = st.secrets.get("NAVER_SECRET_KEY", "")
NAVER_CUSTOMER_ID = str(st.secrets.get("NAVER_CUSTOMER_ID", ""))
GEMINI_API_KEY = "AIzaSyBD_LEBVFv-5nkWXa132iTzpPoXT7RTWf0"
KAKAO_ACCESS_TOKEN = "카카오_토큰을_여기에_입력하세요"

# ==========================================
# [SLOT 1] 글로벌 세션 상태 초기화 및 방어벽 선언
# ==========================================
if 'api_sync_timestamp' not in st.session_state: st.session_state.api_sync_timestamp = "동기화 전"
if 'api_data_period' not in st.session_state: st.session_state.api_data_period = "집계 대기"
if 'campaign_list_raw' not in st.session_state: st.session_state.campaign_list_raw = []
if 'df_clean_data' not in st.session_state: st.session_state.df_clean_data = None
if 'place_diagnosis_data' not in st.session_state: st.session_state.place_diagnosis_data = {}
if 'daily_flow_data' not in st.session_state: st.session_state.daily_flow_data = {}
if 'merged_df' not in st.session_state: st.session_state.merged_df = None
if 'monitoring_report' not in st.session_state: st.session_state.monitoring_report = ""
if 'place_7d_flow' not in st.session_state: st.session_state.place_7d_flow = {}
if 'naver_balance_val' not in st.session_state: st.session_state.naver_balance_val = 0

# 💡 KeyError 원천 차단: 수동 오버라이드용 세션 변수를 최상단에서 강제 선언합니다.
# ▼ 이 4줄을 찾아서 아예 지워버리세요.
place_locations = ["마곡", "가양", "양천향교", "김포공항", "강남", "안산", "인천", "일산"]
for loc in place_locations:
    if f"sb_val_{loc}" not in st.session_state:
        st.session_state[f"sb_val_{loc}"] = "미입력 (API 기준)"
# ==========================================
# [SLOT 2] 파이어베이스 클라우드 통로 초기화
# ==========================================
if not firebase_admin._apps:
    try:
        key_dict = dict(st.secrets["firebase"])
        cred = credentials.Certificate(key_dict)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        st.error(f"파이어베이스 연결 실패: {e}")

try:
    db = firestore.client()
except Exception:
    db = None

def sync_to_firebase(payload_data):
    if not db:
        st.error("❌ 파이어베이스 기지국 연결 실패")
        return
    try:
        db.collection("rentcar_data").document("main_dashboard").set(payload_data)
        st.success("☁️ 파이어베이스(클라우드 DB) 전송 완료! (Vercel 즉시 동기화)")
    except Exception as e:
        st.error(f"❌ 데이터 전송 장애 발생: {e}")

def load_place_ranks():
    if db:
        try:
            doc = db.collection("rentcar_data").document("place_ranks").get()
            if doc.exists: return doc.to_dict()
        except Exception: pass
    return {}

def save_place_ranks(data):
    if db:
        try: db.collection("rentcar_data").document("place_ranks").set(data)
        except Exception: pass

# ==========================================
# [SLOT 3] 네이버 공식 API 핵심 통신 코어 (429 완벽 방어형)
# ==========================================
def make_naver_request(method, uri):
    timestamp = str(int(time.time() * 1000))
    base_uri = uri.split('?')[0]
    message = timestamp + "." + method + "." + base_uri
    signature = base64.b64encode(hmac.new(NAVER_SECRET_KEY.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()).decode("utf-8")
    
    req = urllib.request.Request("https://api.naver.com" + uri)
    req.add_header("X-Timestamp", timestamp)
    req.add_header("X-API-KEY", NAVER_API_KEY)
    req.add_header("X-Customer", str(NAVER_CUSTOMER_ID))
    req.add_header("X-Signature", signature)
    return req

def fetch_naver_bizmoney():
    try:
        req = make_naver_request("GET", "/billing/bizmoney")
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.getcode() == 200:
                res_data = json.loads(response.read().decode("utf-8"))
                val = int(res_data.get("bizmoney", 0))
                if val > 0:
                    st.session_state.naver_balance_val = val
                    return val
    except urllib.error.HTTPError as e:
        if e.code == 429: st.warning("⚠️ 네이버 API 일일 제한 한도 도달 (자정 초기화)")
    except Exception: pass
    return st.session_state.get('naver_balance_val', 0)

def get_all_naver_campaigns():
    try:
        req = make_naver_request("GET", "/ncc/campaigns")
        with urllib.request.urlopen(req, timeout=5) as res:
            return json.loads(res.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        if e.code == 429: return None, "API_LIMIT_EXCEEDED"
        return None, str(e)
    except Exception as e: return None, str(e)

def fetch_campaign_stat_api(camp_id, target_date):
    try:
        time_range_str = json.dumps({"since": target_date, "until": target_date})
        # fields에 avgRnk 추가
        req = make_naver_request("GET", f"/stats?idType=CAMPAIGN&id={camp_id}&fields=%5B%22clkCnt%22%2C%22impCnt%22%2C%22salesAmt%22%2C%22avgRnk%22%5D&timeRange={urllib.parse.quote(time_range_str)}")
        with urllib.request.urlopen(req, timeout=5) as res:
            res_data = json.loads(res.read().decode("utf-8"))
            data_list = res_data.get("data", [])
            if data_list:
                stat = data_list[0]
                clicks = int(stat.get("clkCnt", 0))
                imps = int(stat.get("impCnt", 0))
                spend = int(stat.get("salesAmt", 0))
                avg_rank = float(stat.get("avgRnk", 0.0)) # 순위 데이터 추출
                ctr = (clicks / imps * 100) if imps > 0 else 0.0
                return {"spend": spend, "clicks": clicks, "ctr": ctr, "avg_rank": avg_rank}
    except Exception: pass
    return {"spend": 0, "clicks": 0, "ctr": 0.0, "avg_rank": 0.0}

def fetch_period_stat_api(camp_id, start_dt, end_dt):
    time_range_str = f'{{"since":"{start_dt}","until":"{end_dt}"}}'
    uri = f"/stats?idType=CAMPAIGN&id={camp_id}&fields=%5B%22clkCnt%22%2C%22impCnt%22%2C%22salesAmt%22%5D&timeRange={urllib.parse.quote(time_range_str)}"
    spend, imp, clk = 0, 0, 0
    for _ in range(3):
        try:
            req = make_naver_request("GET", uri)
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.getcode() == 200:
                    raw_json = json.loads(response.read().decode("utf-8"))
                    stat_list = raw_json.get("data", []) if isinstance(raw_json, dict) else raw_json
                    if stat_list:
                        for row in stat_list:
                            spend += int(float(row.get("salesAmt", 0)))
                            imp += int(float(row.get("impCnt", 0)))
                            clk += int(float(row.get("clkCnt", 0)))
                    break
        except Exception: time.sleep(0.5)
    ctr = (clk / imp * 100) if imp > 0 else 0.0
    cpc = int(spend / clk) if clk > 0 else 0
    return spend, clk, ctr, cpc

# ==========================================
# [SLOT 4] 구글 스프레드시트 실시간 재고 파싱 엔진
# ==========================================
def load_smart_spreadsheet(source_path):
    try:
        if "docs.google.com/spreadsheets" in source_path:
            sheet_id = source_path.split("/d/")[1].split("/")[0]
            gid = "0"
            if "gid=" in source_path: gid = source_path.split("gid=")[1].split("&")[0].split("#")[0]
            download_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx&gid={gid}"
            df = pd.read_excel(download_url)
        else:
            df = pd.read_csv(source_path) if source_path.endswith('.csv') else pd.read_excel(source_path)
        
        header_idx = None
        for idx, row in df.iterrows():
            if any('구분' in str(v) or '차종' in str(v) for v in row.values):
                header_idx = idx
                break
                
        if header_idx is not None:
            raw_cols = [str(c).strip() for c in df.iloc[header_idx].values]
            cols = []
            seen = {}
            for c in raw_cols:
                if c in seen: seen[c] += 1; cols.append(f"{c}_{seen[c]}")
                else: seen[c] = 0; cols.append(c)
            
            cleaned_df = df.iloc[header_idx + 1:].copy()
            cleaned_df.columns = cols
            base_col, car_col, no_col = [c for c in cleaned_df.columns if '구분' in c][0], [c for c in cleaned_df.columns if '차종' in c][0], [c for c in cleaned_df.columns if '차량번호' in c][0]
            cleaned_df[base_col] = cleaned_df[base_col].ffill()
            cleaned_df = cleaned_df.dropna(subset=[car_col, no_col])
            
            def fix_cat(row):
                c_name = str(row[car_col]).upper().replace(" ", "")
                if any(x in c_name for x in ["G80", "G90", "K8", "IG", "GN7", "그랜저", "더뉴IG"]): return "대형"
                if any(x in c_name for x in ["K5", "DN8", "CN7", "쏘나타", "아반떼"]): return "중형"
                if any(x in c_name for x in ["니로", "쏘렌토", "MQ4", "스포티지", "투싼", "팰리세이드", "싼타페", "산타페"]): return "SUV"
                if any(x in c_name for x in ["카니발", "스타리아", "스타렉스"]): return "RV/승합"
                if any(x in c_name for x in ["모닝", "캐스퍼", "레이", "0.8"]): return "경차"
                return str(row[base_col]).replace("그룹", "").replace("수량", "").strip()
                
            cleaned_df[base_col] = cleaned_df.apply(fix_cat, axis=1)
            return cleaned_df, None
        return df, None
    except Exception as e: return None, str(e)

# ==========================================
# 📊 [SLOT 5] 대시보드 마스터 레이아웃 렌더러
# ==========================================
st.markdown("""
<div style='background-color:#1E293B; padding:15px; border-radius:8px; display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);'>
    <div style='color:#F8FAFC; font-size:22px; font-weight:bold;'>🚀 빌려타렌트카 권역별 마케팅 통합 지휘소</div>
    <div style='text-align:right;'>
        <div style='color:#94A3B8; font-size:12px;'>네이버 공식 API 집계 기준: <span style='color:#38BDF8; font-weight:bold;'>{period}</span></div>
        <div style='color:#94A3B8; font-size:12px;'>마지막 동기화 시각: <span style='color:#10B981; font-weight:bold;'>{sync_time}</span></div>
    </div>
</div>
""".format(period=st.session_state.api_data_period, sync_time=st.session_state.api_sync_timestamp), unsafe_allow_html=True)

kpi_col1, kpi_col2, kpi_col3 = st.columns(3)
with kpi_col1:
    st.metric(label="네이버 광고 비즈머니 충전 잔액", value=f"{fetch_naver_bizmoney():,} 원")
with kpi_col2:
    target_text = "동기화 대기중"
    if st.session_state.df_clean_data is not None:
        df_t = st.session_state.df_clean_data
        b_col = [c for c in df_t.columns if '구분' in c][0]
        highest_cat = df_t[b_col].value_counts().idxmax()
        highest_count = df_t[b_col].value_counts().max()
        target_text = f"{highest_cat} ({highest_count}대 보유)"
    st.metric(label="현재 가용 재고 1순위 (집중 타겟)", value=target_text)
with kpi_col3:
    st.metric(label="시스템 진단 모드", value="하이브리드 마스터 락")

st.markdown("---")

# ─── 1구역: 실시간 재고 ───
st.markdown('<div class="section-box">', unsafe_allow_html=True)
st.markdown('<div style="background: linear-gradient(90deg, #1E3A8A, #3B82F6); color: white; padding: 14px 20px; border-radius: 8px; font-size: 21px; font-weight: bold; margin-bottom: 20px;">🚘 1. 실시간 재고 집계 관제</div>', unsafe_allow_html=True)
my_real_sheet = "https://docs.google.com/spreadsheets/d/1cyA7UB5wCiq58z6G103qcFLpKDjMyGKiRRkmTvGWLTk/edit?gid=0#gid=0"
if st.button("실시간 재고 통계 동기화 실행", key="sync_btn", type="primary"):
    df, err = load_smart_spreadsheet(my_real_sheet)
    if err: st.error(f"데이터 동기화 실패: {err}")
    else:
        st.session_state.df_clean_data = df
        st.rerun()

if st.session_state.df_clean_data is not None:
    df_target = st.session_state.df_clean_data
    base_col = [c for c in df_target.columns if '구분' in c][0]
    car_col = [c for c in df_target.columns if '차종' in c][0]
    category_counts = df_target[base_col].value_counts()
    cat_cols = st.columns(4)
    for idx, (cat_name, count_val) in enumerate(category_counts.items()):
        with cat_cols[idx % 4]:
            sub_cars = df_target[df_target[base_col] == cat_name][car_col].value_counts()
            list_items = "".join([f"<div style='font-size:13px; color:#475569; padding:6px 0; border-bottom:1px dashed #E2E8F0;'>▪️ {c_name} <span style='float:right; font-weight:bold; color:#0F172A;'>{c_cnt}대</span></div>" for c_name, c_cnt in sub_cars.items()])
            st.markdown(f"<div style='background-color:#FFFFFF; border:1px solid #CBD5E1; border-radius:8px; padding:15px; margin-bottom:15px;'><div style='font-size:17px; font-weight:bold; color:#1E3A8A; border-bottom:2px solid #3B82F6; padding-bottom:10px; margin-bottom:10px;'>{cat_name} <span style='float:right; background-color:#EFF6FF; color:#1D4ED8; padding:2px 8px; border-radius:12px; font-size:13px;'>총 {count_val}대</span></div><div style='max-height:180px; overflow-y:auto;'>{list_items}</div></div>", unsafe_allow_html=True)
st.markdown('</div>', unsafe_allow_html=True)

# ─── 2구역: 플레이스 광고 ───
st.markdown('<div class="section-box">', unsafe_allow_html=True)
st.markdown('<div style="background: linear-gradient(90deg, #1E3A8A, #3B82F6); color: white; padding: 14px 20px; border-radius: 8px; font-size: 21px; font-weight: bold; margin-bottom: 20px;">🎯 2. 플레이스 광고 현황</div>', unsafe_allow_html=True)

total_place_spend_selected = sum([d.get("spend", 0) for d in st.session_state.place_diagnosis_data.values()]) if st.session_state.place_diagnosis_data else 0
total_place_spend_7days = sum(st.session_state.place_7d_flow.values()) if st.session_state.place_7d_flow else 0

col1, col2 = st.columns(2)
with col1: st.metric(label="선택일 기준 플레이스 총 지출액", value=f"{total_place_spend_selected:,} 원")
with col2: st.metric(label="최근 7일 누적 플레이스 총 지출액", value=f"{total_place_spend_7days:,} 원")

st.markdown("<div style='font-size:14px; font-weight:bold; color:#334155; margin-bottom:5px;'>📅 데이터 조회 기준일 선택</div>", unsafe_allow_html=True)
stat_option = st.radio("API 통계 추출 기준일 선택", ["오늘 (현재까지의 실시간 누적)", "어제 (최종 마감 팩트)"], horizontal=True, label_visibility="collapsed")
stat_target_date = datetime.date.today().strftime('%Y-%m-%d') if "오늘" in stat_option else (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
display_date_label = "오늘" if "오늘" in stat_option else "어제"

if st.button(f"📊 네이버 플레이스 성적표 동기화 (기준일: {stat_target_date})", key="place_sync_btn", type="primary"):
    with st.spinner("🚀 네이버 플레이스 스캔 중..."):
        all_camps, err = get_all_naver_campaigns()
        if err == "API_LIMIT_EXCEEDED": st.error("🚨 네이버 API 호출 한도 초과 (자정 해제)")
        elif all_camps:
            place_camps = [c for c in all_camps if str(c.get("campaignTp", c.get("type", ""))).upper() in ["LOCAL_AD", "PLACE"] or any(x in str(c.get("name", "")).replace(" ", "") for x in ["플레이스", "플레", "지역"])]
            master_place_adgroups = []
            
            def fetch_adgroup_parallel(p_camp):
                cid, cname = p_camp.get("nccCampaignId"), p_camp.get("name")
                camp_lock, camp_status = str(p_camp.get("userLock", "")).strip().upper(), str(p_camp.get("status", "")).strip().upper()
                camp_on = not (camp_lock in ["PAUSED", "STOPPED"] or camp_status in ["PAUSED", "STOPPED"])
                res_list = []
                try:
                    req_ag = make_naver_request("GET", f"/ncc/adgroups?nccCampaignId={cid}")
                    with urllib.request.urlopen(req_ag, timeout=5) as res_ag:
                        for ag in json.loads(res_ag.read().decode("utf-8")):
                            ag["_cid"], ag["_camp_name"], ag["_camp_on"] = cid, cname, camp_on
                            res_list.append(ag)
                except Exception: pass
                return res_list

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                for res in executor.map(fetch_adgroup_parallel, place_camps): master_place_adgroups.extend(res)

            results, cids_for_7d, loc_cids_map = {}, [], {}
            for loc in place_locations:
                active_bids, paused_bids, cids_to_check = [], [], []
                is_any_on = False
                for ag in master_place_adgroups:
                    if loc in str(ag["_camp_name"]).replace(" ", "") or loc in str(ag.get("name", "")).replace(" ", ""):
                        cid = ag["_cid"]
                        if cid not in cids_to_check: cids_to_check.append(cid)
                        if cid not in cids_for_7d: cids_for_7d.append(cid)
                        ag_on = str(ag.get("userLock", "")).strip().upper() not in ["PAUSED", "STOPPED"] and str(ag.get("status", "")).strip().upper() not in ["PAUSED", "STOPPED"]
                        ag_bid = int(ag.get("bidAmt", 0))
                        if ag["_camp_on"] and ag_on: active_bids.append(ag_bid); is_any_on = True
                        else: paused_bids.append(ag_bid)
                
                bid = max(active_bids) if active_bids else (max(paused_bids) if paused_bids else 0)
                loc_cids_map[loc] = {"bid": bid, "is_on": is_any_on, "cids": cids_to_check}
                
            date_list = [(datetime.date.today() - datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(6, -1, -1)]
            stat_queries = set()
            for loc, ldata in loc_cids_map.items():
                for cid in ldata["cids"]: stat_queries.add((cid, stat_target_date))
            for cid in cids_for_7d:
                for d in date_list: stat_queries.add((cid, d))

            def fetch_stat_runner(cid, date):
                try: return cid, date, fetch_campaign_stat_api(cid, date)
                except Exception: return cid, date, {'spend':0, 'clicks':0, 'avg_rank':0.0}

            stat_results_dict = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                for future in concurrent.futures.as_completed([executor.submit(fetch_stat_runner, q[0], q[1]) for q in stat_queries]):
                    c, d, stat = future.result()
                    stat_results_dict[(c, d)] = stat
            
            for loc, ldata in loc_cids_map.items():
                tot_spend, tot_clicks, sum_rank, active_rank_cnt = 0, 0, 0.0, 0
                for cid in ldata["cids"]:
                    stat = stat_results_dict.get((cid, stat_target_date), {'spend':0, 'clicks':0, 'avg_rank':0.0})
                    tot_spend += stat['spend']
                    tot_clicks += stat['clicks']
                    if stat['avg_rank'] > 0: sum_rank += stat['avg_rank']; active_rank_cnt += 1
                
                results[loc] = {
                    "bid": ldata["bid"], "is_on": ldata["is_on"],
                    "avg_rank": sum_rank / active_rank_cnt if active_rank_cnt > 0 else 0.0,
                    "spend": tot_spend, "clicks": tot_clicks, "date_label": display_date_label
                }
            st.session_state.place_diagnosis_data = results
            
            place_7d_data = {d: 0 for d in date_list}
            for d in date_list:
                place_7d_data[d] = sum([stat_results_dict.get((cid, d), {'spend':0})['spend'] for cid in cids_for_7d])
            st.session_state.place_7d_flow = place_7d_data
            st.session_state.api_sync_timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            st.session_state.api_data_period = display_date_label
            st.rerun()

if st.session_state.place_diagnosis_data:
    
            saved_ranks_dict = load_place_ranks()
        
        # 💡 [핵심] 새로고침 시 파이어베이스에 저장된 순위를 최우선으로 가져오도록 복구
        for loc in place_locations:
            if f"sb_val_{loc}" not in st.session_state:
                st.session_state[f"sb_val_{loc}"] = saved_ranks_dict.get(loc, "미입력 (API 기준)")

        # 깜빡임 원천 봉쇄 콜백
        def update_rank_callback(loc_name):
            new_val = st.session_state[f"ui_sb_{loc_name}"]
            st.session_state[f"sb_val_{loc_name}"] = new_val
            saved_ranks_dict[loc_name] = new_val
            save_place_ranks(saved_ranks_dict)

        cols = st.columns(4)
        for idx, loc in enumerate(place_locations):
            data = st.session_state.place_diagnosis_data.get(loc, {})
            if not data: continue
            with cols[idx % 4]:
                st.markdown(f"<div style='background-color:#F8FAFC; padding:8px; border-radius:6px; border-left:4px solid #1E3A8A; margin-bottom:10px;'><b style='font-size:15px; color:#0F172A;'>📍 [지점] {loc}</b></div>", unsafe_allow_html=True)
                naver_search_url = f"https://m.search.naver.com/search.naver?where=m&query={urllib.parse.quote(loc + ' 렌트카')}"
                st.markdown(f"<a href='{naver_search_url}' target='_blank' style='display:block; text-align:center; background-color:#22C55E; color:white; padding:8px; border-radius:4px; text-decoration:none; font-size:12px; font-weight:bold; margin-bottom:10px;'>현장 모바일 1초 즉시 확인</a>", unsafe_allow_html=True)
                
                # 에러 유발 요인 제거 및 옵션 리스트 명시
                options_list = ["미입력 (API 기준)", "1위", "2위", "3위", "순위 밖"]
                try: 
                    default_idx = options_list.index(st.session_state[f"sb_val_{loc}"])
                except: 
                    default_idx = 0
                
                override_val = st.selectbox("순위 덮어쓰기 (Vercel 즉시 반영)", options_list, index=default_idx, key=f"ui_sb_{loc}", on_change=update_rank_callback, args=(loc,))
            is_manual = override_val != "미입력 (API 기준)"
            display_rank = override_val if is_manual else f"평균 {data['avg_rank']:.1f}위"
            
            bg, border, text = "#F8FAFC", "#CBD5E1", "#475569" 
            if "1" in display_rank: bg, border, text = "#ECFDF5", "#10B981", "#047857"
            elif "2" in display_rank or "3" in display_rank: bg, border, text = "#EFF6FF", "#3B82F6", "#1D4ED8"
            elif "밖" in display_rank or (not is_manual and data['avg_rank'] > 3.0): bg, border, text = "#FEF2F2", "#EF4444", "#B91C1C"

            st.markdown(f"""
            <div style="background-color:{bg}; border:2px solid {border}; border-radius:8px; padding:12px; text-align:center; margin-bottom:10px;">
                <div style="font-size:11px; color:{text}; margin-bottom:2px;">{'[수동] 실시간 팩트' if is_manual else f'네이버 API ({data.get("date_label","어제")} 평균)'}</div>
                <div style="font-size:18px; font-weight:bold; color:{text};">{display_rank}</div>
            </div>
            <div style='font-size:12px; color:#334155; line-height:1.6; background:#F1F5F9; padding:8px; border-radius:4px;'>
                - 단가: <b>{data.get('bid', 0):,}원</b> ({'ON' if data.get('is_on') else 'OFF'})<br>
                - 비용: <b>{data.get('spend', 0):,}원</b><br>
                - 유입: <b>{data.get('clicks', 0)}건</b>
            </div>
            """, unsafe_allow_html=True)
            
            advice = ""
            current_rank_val = 99 if ("밖" in display_rank or (not is_manual and data['avg_rank'] > 3.0)) else float(re.findall(r"[\d.]+", display_rank)[0]) if re.findall(r"[\d.]+", display_rank) else 99
            current_hour = datetime.datetime.now().hour
            pacing_warn = "<br><br><b>[예산 페이스 조절]</b> 오전 소진 속도가 과도합니다. 단가를 10% 하향 조절하십시오." if (current_hour <= 13 and data.get('spend',0) >= 15000) else ""

            if data.get('bid',0) >= 4500 and current_rank_val >= 3: advice = f"<b>[품질지수 보정]</b> 단가 상한선 임박. 가격 경쟁을 중단하고 문구를 <u>'추가금 0원'</u>으로 변경하십시오.{pacing_warn}"
            elif current_rank_val <= 2.0 and data.get('clicks',0) <= 2 and data.get('spend',0) > 0: advice = f"<b>[상품군 스위칭]</b> 유입 저조. <b>C1(법인) 또는 C3(저신용)</b> 영업용 피드로 교체하십시오.{pacing_warn}"
            elif loc in ["마곡", "가양", "양천향교"]:
                if current_rank_val <= 1.5: advice = f"<b>[코어 장악 및 확장]</b> 독점 중. <u>영등포, 구로</u> 권역까지 범위를 넓혀 수요를 흡수하십시오.{pacing_warn}"
                else: advice = f"<b>[우회 전술]</b> 경쟁 과열. 반경 2km 내 <b>화곡역, 등촌동</b> 타겟팅을 침투시키십시오.{pacing_warn}"
            elif loc == "김포공항": advice = f"<b>[타 지역 인터셉트]</b> 검색 수요 전국구. 노출 지역에 <b>'인천 계양구, 일산 동구'</b>를 강제 연동하십시오."
            elif loc in ["인천", "안산", "일산"]: advice = f"<b>[광역 공백 방어]</b> 유입 밀림 시 인접 배후 지역까지 노출 범위를 과감히 넓히십시오.{pacing_warn}"
            elif loc == "강남":
                if current_rank_val <= 2.0: advice = f"<b>[비즈니스 확장]</b> <b>서초구, 판교</b>까지 패키지로 확장하여 객단가를 극대화하십시오."
                else: advice = f"<b>[우회]</b> 경쟁 밀림 시 <u>'서초 장기렌트카'</u> 등 인접 롱테일 키워드로 전환하십시오.{pacing_warn}"
            else: advice = f"<b>[신규 모니터링]</b> 정체 시 즉시 <b>C3(무심사)</b> 피드로 방어하십시오.{pacing_warn}"

            st.markdown(f"<div style='font-size:12px; background-color:#FFFBEB; padding:10px; border-left:4px solid #D97706; margin-top:8px; border-radius:4px; line-height:1.6;'><b>[마스터 작전 지침]</b><br>{advice}</div><br>", unsafe_allow_html=True)

st.markdown('</div>', unsafe_allow_html=True)

# ─── 3구역: 파워링크 광고 ───
st.markdown('<div class="section-box">', unsafe_allow_html=True)
st.markdown('<div style="background: linear-gradient(90deg, #1E3A8A, #3B82F6); color: white; padding: 14px 20px; border-radius: 8px; font-size: 21px; font-weight: bold; margin-bottom: 20px;">3. 파워링크 광고 현황</div>', unsafe_allow_html=True)

total_power_spend_today = 0
total_power_spend_7days = 0
if st.session_state.daily_flow_data:
    sorted_dates = sorted(st.session_state.daily_flow_data.keys())
    total_power_spend_7days = sum([st.session_state.daily_flow_data[d].get("파워링크", 0) for d in sorted_dates])
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    total_power_spend_today = st.session_state.daily_flow_data.get(today_str, {}).get("파워링크", 0) if today_str in st.session_state.daily_flow_data else st.session_state.daily_flow_data[sorted_dates[-1]].get("파워링크", 0)

col1, col2 = st.columns(2)
with col1: st.metric(label="오늘 파워링크 총 지출액", value=f"{total_power_spend_today:,} 원")
with col2: st.metric(label="최근 7일 누적 파워링크 총 지출액", value=f"{total_power_spend_7days:,} 원")

default_end = datetime.date.today() - datetime.timedelta(days=1)
default_start = default_end - datetime.timedelta(days=2)
date_range = st.date_input("대조군 검증 대상 기간 (조정 후)", value=(default_start, default_end))

if st.button("📊 파워링크 성적표 동기화 실행", key="power_sync_btn", type="primary"):
    if isinstance(date_range, tuple) and len(date_range) == 2:
        with st.spinner("🚀 파워링크 스캔 중..."):
            post_start_dt, post_end_dt = date_range[0], date_range[1]
            duration_days = (post_end_dt - post_start_dt).days + 1
            pre_start_str = (post_start_dt - datetime.timedelta(days=duration_days)).strftime('%Y-%m-%d')
            pre_end_str = (post_start_dt - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
            post_start_str, post_end_str = post_start_dt.strftime('%Y-%m-%d'), post_end_dt.strftime('%Y-%m-%d')
            
            all_camps, err = get_all_naver_campaigns()
            if err == "API_LIMIT_EXCEEDED": st.error("🚨 네이버 API 호출 한도 초과")
            elif all_camps:
                today = datetime.date.today()
                date_list = [(today - datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(6, -1, -1)]
                
                # 플레이스 제외하고 파워링크만 남기기
                power_camps = []
                for c in all_camps:
                    ctype = str(c.get("campaignTp", c.get("type", "WEB_SITE"))).upper()
                    cname = str(c.get("name", ""))
                    if "파워링크" in cname or (ctype not in ["PLACE", "LOCAL_AD"] and "플레이스" not in cname):
                        power_camps.append(c)

                bot_records, daily_flow = [], {d: {"파워링크": 0} for d in date_list}

                def process_campaign_parallel(camp):
                    cid, cname = camp.get("nccCampaignId"), camp.get("name")
                    short_name = re.sub(r'\s*\(.*?\)', '', cname).replace("파워링크#", "").strip()
                    pre_res = fetch_period_stat_api(cid, pre_start_str, pre_end_str)
                    post_res = fetch_period_stat_api(cid, post_start_str, post_end_str)
                    day_res = {d: fetch_period_stat_api(cid, d, d) for d in date_list}
                    return {"short_name": short_name, "pre": pre_res, "post": post_res, "days": day_res}

                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                    for res in executor.map(process_campaign_parallel, power_camps):
                        sp_pre, _, ctr_pre, cpc_pre = res["pre"]
                        sp_post, clk_post, ctr_post, cpc_post = res["post"]
                        if sp_pre > 0 or sp_post > 0:
                            bot_records.append({"캠페인명": res["short_name"], "광고종류": "파워링크", "조정 전 비용": sp_pre, "클릭률_전": ctr_pre, "CPC_전": cpc_pre, "조정 후 비용": sp_post, "클릭률_후": ctr_post, "CPC_후": cpc_post, "클릭수_후": clk_post})
                            for d in date_list: daily_flow[d]["파워링크"] += res["days"][d][0]

                st.session_state.merged_df = pd.DataFrame(bot_records)
                st.session_state.daily_flow_data = daily_flow
                st.success("✅ 파워링크 연동 및 전후 분석 완료!")
                st.rerun()

if st.session_state.merged_df is not None and not st.session_state.merged_df.empty:
    st.markdown("<br><div style='font-size:16px; font-weight:bold;'>📉 파워링크 성과 상세 대조표</div>", unsafe_allow_html=True)
    html_table = "<div style='width:100%; overflow-x:auto; background-color:#FFFFFF !important; border:1px solid #E2E8F0; border-radius:8px;'><table style='width:100%; min-width:850px; text-align:center; border-collapse:collapse; color:#0F172A !important;'><thead style='background-color:#F8FAFC !important; border-bottom:2px solid #CBD5E1;'><tr><th style='padding:12px; font-size:13px;'>캠페인명</th><th style='padding:12px; font-size:13px;'>이전 비용</th><th style='padding:12px; font-size:13px;'>이후 비용</th><th style='padding:12px; font-size:13px;'>이전 CPC</th><th style='padding:12px; font-size:13px;'>이후 CPC</th><th style='padding:12px; font-size:13px;'>최종 조치</th><th style='padding:12px; font-size:13px;'>진단 사유</th></tr></thead><tbody>"
    for _, r in st.session_state.merged_df.iterrows():
        ctr_diff, cpc_diff = r["클릭률_후"] - r["클릭률_전"], r["CPC_후"] - r["CPC_전"]
        if ctr_diff > 0 and cpc_diff <= 0: cond, c, reason = "유지 (우수)", "#16A34A", "클릭률 상승 및 단가 절감"
        elif ctr_diff <= 0 and cpc_diff > 0: cond, c, reason = "수정 필요", "#DC2626", "클릭률 하락 및 단가 인상"
        else: cond, c, reason = "모니터링", "#475569", "오차 범위 내 균형 상태"
        html_table += f"<tr style='border-bottom:1px solid #F1F5F9;'><td style='padding:10px; font-size:13px; font-weight:bold; text-align:left;'>{r['캠페인명']}</td><td>{int(r['조정 전 비용']):,}원</td><td>{int(r['조정 후 비용']):,}원</td><td>{int(r['CPC_전']):,}원</td><td>{int(r['CPC_후']):,}원</td><td style='font-weight:bold; color:{c};'>{cond}</td><td style='text-align:left; font-size:12px;'>{reason}</td></tr>"
    st.markdown(html_table + "</tbody></table></div>", unsafe_allow_html=True)
st.markdown('</div>', unsafe_allow_html=True)

# ─── 4구역: 파이어베이스 배달 ───
st.markdown('<div class="section-box">', unsafe_allow_html=True)
st.markdown('<div style="background: linear-gradient(90deg, #10B981, #059669); color: white; padding: 14px 20px; border-radius: 8px; font-size: 21px; font-weight: bold; margin-bottom: 20px;">🚀 4. 최종 데이터 파이어베이스(Vercel) 송출</div>', unsafe_allow_html=True)

if st.button("🌟 모든 데이터 모아서 Vercel로 보내기", key="vercel_sync_btn", type="primary"):
    with st.spinner("파이어베이스 원격 게이트웨이를 여는 중..."):
        payload = {
            "inventory": {"totalCars": int(st.session_state.df_clean_data.shape[0]) if st.session_state.df_clean_data is not None else 0, "lastSync": datetime.datetime.now().strftime("%H:%M:%S"), "bizMoney": f"{st.session_state.get('naver_balance_val', 0):,}원", "categories": {}},
            "placeSummary": {"totalSpend": total_place_spend_selected, "sevenDayTotal": total_place_spend_7days},
            "placeLocations": [],
            "placeFlow": [{"date": d, "spend": v} for d, v in sorted(st.session_state.get('place_7d_flow', {}).items())],
            "powerlinkSummary": {"totalSpend": total_power_spend_today, "sevenDayTotal": total_power_spend_7days},
            "powerlinkRows": [], "powerlinkCompare": [],
            "powerlinkFlow": [{"date": d, "spend": v.get("파워링크", 0)} for d, v in sorted(st.session_state.get('daily_flow_data', {}).items())],
            "aiReport": "Vercel 실시간 마케팅 연동 파이프라인 정상 가동."
        }
        if st.session_state.df_clean_data is not None:
            df = st.session_state.df_clean_data
            target_col = [col for col in ['차급', '차종', '분류', '구분', '차량구분'] if col in df.columns]
            if target_col: payload["inventory"]["categories"] = {str(k): int(v) for k, v in df[target_col[0]].value_counts().to_dict().items()}
        
        saved_ranks = load_place_ranks()
        if st.session_state.place_diagnosis_data:
            for loc, d in st.session_state.place_diagnosis_data.items():
                payload["placeLocations"].append({"id": loc, "name": loc, "status": "운영중" if d.get('is_on') else "대기중", "rank": saved_ranks.get(loc, f"평균 {d.get('avg_rank',0):.1f}위"), "spend": d.get('spend', 0), "sales": d.get('spend', 0) * 8, "count": d.get('clicks', 0), "advice": "정상 구동 중"})
        
        if st.session_state.merged_df is not None and not st.session_state.merged_df.empty:
            p_data = st.session_state.merged_df[st.session_state.merged_df["광고종류"] == "파워링크"]
            for idx, r in p_data.iterrows():
                payload["powerlinkRows"].append({"id": str(idx), "keyword": r.get('캠페인명', ''), "status": "운영중", "rank": 0, "bid": r.get('CPC_후', 0), "spend": r.get('조정 후 비용', 0), "clicks": r.get('클릭수_후', 0), "action": "keep"})
        
        sync_to_firebase(payload)
st.markdown('</div>', unsafe_allow_html=True)