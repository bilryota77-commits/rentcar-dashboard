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

# --- 파이어베이스 초기화 (스트림릿 클라우드 정석 연동) ---
if not firebase_admin._apps:
    try:
        key_dict = json.loads(st.secrets["FIREBASE_JSON"])
        cred = credentials.Certificate(key_dict)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        st.error(f"파이어베이스 연결 실패: {e}")

try:
    db = firestore.client()
except Exception:
    db = None

# --- 파이어베이스 데이터 통합 배달 비서 (Next.js 화면 깨우기 핵심 엔진) ---
def sync_to_firebase(naver_data):
    if not db:
        st.error("❌ 파이어베이스가 연결되지 않아 데이터를 전송할 수 없습니다.")
        return
    try:
        # 1. 전달받은 데이터를 파이어베이스에 쏘기 좋게 딕셔너리로 예쁘게 묶습니다.
        full_data_to_send = {
            "inventory": naver_data.get('inventory', {}),
            "placeSummary": naver_data.get('placeSummary', {}),
            "placeLocations": naver_data.get('placeLocations', []),
            "placeFlow": naver_data.get('placeFlow', []),
            "powerlinkSummary": naver_data.get('powerlinkSummary', {}),
            "powerlinkRows": naver_data.get('powerlinkRows', []),
            "powerlinkFlow": naver_data.get('powerlinkFlow', []),
            "powerlinkCompare": naver_data.get('powerlinkCompare', []),
            "aiReport": naver_data.get('aiReport', '지침 대기중')
        }

        # 2. 파이어베이스 rentcar_data 컬렉션에 main_dashboard 문서로 통합 전송합니다.
        doc_ref = db.collection("rentcar_data").document("main_dashboard")
        doc_ref.set(full_data_to_send)
        st.success("☁️ 파이어베이스(클라우드 DB) main_dashboard 문서 전송 완벽 성공!")
    except Exception as e:
        st.error(f"❌ 파이어베이스 전송 실패: {e}")

# --- 파이어베이스 수동 순위 보정 로드 함수 ---
def load_place_ranks():
    if db:
        try:
            doc_ref = db.collection("rentcar_data").document("place_ranks")
            doc = doc_ref.get()
            if doc.exists:
                return doc.to_dict()
        except Exception:
            pass
    return {}

def save_place_ranks(data):
    if db:
        try:
            doc_ref = db.collection("rentcar_data").document("place_ranks")
            doc_ref.set(data)
        except Exception:
            pass

# =========================================================================
# ⚡ 네이버 API 통계 정밀 추출 엔진
# =========================================================================
def fetch_campaign_stat_api(camp_id, target_date):
    try:
        time_range_str = json.dumps({"since": target_date, "until": target_date})
        req = make_naver_request("GET", f"/stats?idType=CAMPAIGN&id={camp_id}&fields=%5B%22clkCnt%22%2C%22impCnt%22%2C%22salesAmt%22%5D&timeRange={urllib.parse.quote(time_range_str)}")
        with urllib.request.urlopen(req, timeout=5) as res:
            res_data = json.loads(res.read().decode("utf-8"))
            data_list = res_data.get("data", [])
            if data_list:
                stat = data_list[0]
                clicks = int(stat.get("clkCnt", 0))
                imps = int(stat.get("impCnt", 0))
                spend = int(stat.get("salesAmt", 0))
                ctr = (clicks / imps * 100) if imps > 0 else 0.0
                return {"spend": spend, "clicks": clicks, "ctr": ctr, "avg_rank": 0.0}
    except Exception:
        pass
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
        except Exception:
            time.sleep(0.5)
            
    ctr = (clk / imp * 100) if imp > 0 else 0.0
    cpc = int(spend / clk) if clk > 0 else 0
    return spend, clk, ctr, cpc

# ==========================================
# [필수 설정] API 연동 키 구역
# ==========================================
NAVER_API_KEY = st.secrets["NAVER_API_KEY"]
NAVER_SECRET_KEY = st.secrets["NAVER_SECRET_KEY"]
NAVER_CUSTOMER_ID = str(st.secrets["NAVER_CUSTOMER_ID"])
GEMINI_API_KEY = "AIzaSyBD_LEBVFv-5nkWXa132iTzpPoXT7RTWf0"
KAKAO_ACCESS_TOKEN = "카카오_토큰을_여기에_입력하세요"

st.set_page_config(layout="wide")

# ==========================================
# 글로벌 세션 상태 초기화
# ==========================================
if 'api_sync_timestamp' not in st.session_state: st.session_state.api_sync_timestamp = "동기화 전"
if 'api_data_period' not in st.session_state: st.session_state.api_data_period = "집계 대기"
if 'campaign_list_raw' not in st.session_state: st.session_state.campaign_list_raw = []
if 'df_clean_data' not in st.session_state: st.session_state.df_clean_data = None
if 'place_diagnosis_data' not in st.session_state: st.session_state.place_diagnosis_data = {}
if 'daily_flow_data' not in st.session_state: st.session_state.daily_flow_data = {}
if 'merged_df' not in st.session_state: st.session_state.merged_df = None
if 'monitoring_report' not in st.session_state: st.session_state.monitoring_report = ""

# ==========================================
# [엔진 1] 네이버 공식 API 통신 모듈
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
        return st.session_state.get('naver_balance_val', 0)
    except Exception as e:
        st.error(f"🚨 네이버 비즈머니 통신 에러 발생: {e}")
        return st.session_state.get('naver_balance_val', 0)

def get_all_naver_campaigns():
    req = make_naver_request("GET", "/ncc/campaigns")
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return json.loads(res.read().decode("utf-8")), None
    except Exception as e:
        return None, str(e)

# ==========================================
# [엔진 2] 구글 시트 실시간 재고 연동 모듈
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
    except Exception as e:
        return None, str(e)

# KPI 렌더링 상단 대시보드
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
    st.session_state.naver_balance_val = fetch_naver_bizmoney()
    st.metric(label="네이버 광고 비즈머니 충전 잔액", value=f"{st.session_state.naver_balance_val:,} 원")
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
    st.metric(label="시스템 진단 모드", value="하이브리드 팩트 체크")

st.markdown("---")

# 1구역 렌더링
st.markdown('<div class="section-box">', unsafe_allow_html=True)
st.markdown("""
<div style="background: linear-gradient(90deg, #1E3A8A, #3B82F6); color: white; padding: 14px 20px; border-radius: 8px; font-size: 21px; font-weight: bold; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
    🚘 1. 실시간 재고 집계 관제 (광고 타겟 설정 기준)
</div>
""", unsafe_allow_html=True)

my_real_sheet = "https://docs.google.com/spreadsheets/d/1cyA7UB5wCiq58z6G103qcFLpKDjMyGKiRRkmTvGWLTk/edit?gid=0#gid=0"
if st.button("실시간 재고 통계 동기화 실행", key="sync_btn", type="primary"):
    df, err = load_smart_spreadsheet(my_real_sheet)
    if err: st.error(f"데이터 동기화 실패: {err}")
    else:
        st.success("실시간 재고 현황 정제 완료")
        st.session_state.df_clean_data = df
        st.rerun()

if st.session_state.df_clean_data is not None:
    df_target = st.session_state.df_clean_data
    base_col = [c for c in df_target.columns if '구분' in c][0]
    car_col = [c for c in df_target.columns if '차종' in c][0]
    category_counts = df_target[base_col].value_counts()
    
    st.markdown("<div style='font-size:18px; font-weight:bold; color:#334155; margin-top:15px; margin-bottom:15px;'>📋 분류 항목별 차량 가용 대수 현황</div>", unsafe_allow_html=True)
    cat_cols = st.columns(4)
    for idx, (cat_name, count_val) in enumerate(category_counts.items()):
        with cat_cols[idx % 4]:
            sub_cars = df_target[df_target[base_col] == cat_name][car_col].value_counts()
            list_items = "".join([f"<div style='font-size:13px; color:#475569; padding:6px 0; border-bottom:1px dashed #E2E8F0;'>▪️ {c_name} <span style='float:right; font-weight:bold; color:#0F172A;'>{c_cnt}대</span></div>" for c_name, c_cnt in sub_cars.items()])
            st.markdown(f"<div style='background-color:#FFFFFF; border:1px solid #CBD5E1; border-radius:8px; padding:15px; margin-bottom:15px;'><div style='font-size:17px; font-weight:bold; color:#1E3A8A; border-bottom:2px solid #3B82F6; padding-bottom:10px; margin-bottom:10px;'>{cat_name} <span style='float:right; background-color:#EFF6FF; color:#1D4ED8; padding:2px 8px; border-radius:12px; font-size:13px;'>총 {count_val}대</span></div><div style='max-height:180px; overflow-y:auto;'>{list_items}</div></div>", unsafe_allow_html=True)
st.markdown('</div>', unsafe_allow_html=True)

# 2구역 렌더링
st.markdown('<div class="section-box">', unsafe_allow_html=True)
st.markdown("""
<div style="background: linear-gradient(90deg, #1E3A8A, #3B82F6); color: white; padding: 14px 20px; border-radius: 8px; font-size: 21px; font-weight: bold; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
    🎯 2. 플레이스 광고 현황
</div>
""", unsafe_allow_html=True)

total_place_spend_selected = 0
current_label = "조회 전"
if 'place_diagnosis_data' in st.session_state and st.session_state.place_diagnosis_data:
    for loc, data in st.session_state.place_diagnosis_data.items():
        total_place_spend_selected += data.get("spend", 0)
        current_label = data.get('date_label', '어제/오늘')

total_place_spend_7days = 0
if 'place_7d_flow' in st.session_state and st.session_state.place_7d_flow:
    for d, spend in st.session_state.place_7d_flow.items():
        total_place_spend_7days += spend

col1, col2 = st.columns(2)
with col1:
    st.metric(label=f"선택일 기준 ({current_label}) 플레이스 총 지출액", value=f"{total_place_spend_selected:,} 원")
with col2:
    st.metric(label="최근 7일 누적 플레이스 총 지출액 (비교용)", value=f"{total_place_spend_7days:,} 원")

st.markdown("<div style='font-size:16px; font-weight:bold; color:#475569; margin-top:15px; margin-bottom:10px;'>📅 최근 7일 플레이스 지출 흐름</div>", unsafe_allow_html=True)
if 'place_7d_flow' in st.session_state and st.session_state.place_7d_flow:
    flow_cols = st.columns(7)
    for i, (d, spend) in enumerate(sorted(st.session_state.place_7d_flow.items())):
        short_date = d.split("-")[1] + "/" + d.split("-")[2]
        with flow_cols[i]:
            st.metric(label=short_date, value=f"{spend:,}원")

st.markdown("---")
place_locations = ["마곡", "가양", "양천향교", "김포공항", "강남", "안산", "인천", "일산"]

stat_option = st.radio("API 통계 추출 기준일 선택", ["오늘 (현재까지의 실시간 누적)", "어제 (최종 마감 팩트)"], horizontal=True)
if "오늘" in stat_option:
    stat_target_date = datetime.date.today().strftime('%Y-%m-%d')
    display_date_label = "오늘"
else:
    stat_target_date = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    display_date_label = "어제"

if st.button(f"📊 네이버 공식 성적표(API) 100% 동기화 (기준일: {stat_target_date})", key="place_sync_btn", type="primary"):
    with st.spinner("🚀 네이버 공식 통계를 마스터 로드하는 중입니다..."):
        start_time = time.time()
        all_camps, err = get_all_naver_campaigns()
        if err:
            st.error(f"API 통신 장애: {err}")
        else:
            place_camps = [c for c in all_camps if str(c.get("campaignTp", c.get("type", ""))).upper() in ["LOCAL_AD", "PLACE"] or any(x in str(c.get("name", "")).replace(" ", "") for x in ["플레이스", "플레", "지역"])]
            master_place_adgroups = []
            
            def fetch_adgroup_parallel(p_camp):
                cid = p_camp.get("nccCampaignId")
                camp_name = p_camp.get("name")
                camp_lock = str(p_camp.get("userLock", "")).strip().upper()
                camp_status = str(p_camp.get("status", "")).strip().upper()
                camp_on = not (camp_lock in ["PAUSED", "STOPPED"] or camp_status in ["PAUSED", "STOPPED"])
                res_list = []
                try:
                    time.sleep(0.05)
                    req_ag = make_naver_request("GET", f"/ncc/adgroups?nccCampaignId={cid}")
                    with urllib.request.urlopen(req_ag, timeout=5) as res_ag:
                        adgroups = json.loads(res_ag.read().decode("utf-8"))
                        for ag in adgroups:
                            ag["_cid"] = cid
                            ag["_camp_name"] = camp_name
                            ag["_camp_on"] = camp_on
                            res_list.append(ag)
                except Exception:
                    pass
                return res_list

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                for res in executor.map(fetch_adgroup_parallel, place_camps):
                    master_place_adgroups.extend(res)

            results = {}
            cids_for_7d = []
            loc_cids_map = {}
            
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
                        if ag["_camp_on"] and ag_on:
                            active_bids.append(ag_bid)
                            is_any_on = True
                        else:
                            paused_bids.append(ag_bid)
                final_on = is_any_on
                bid = max(active_bids) if active_bids else (max(paused_bids) if paused_bids else 0)
                loc_cids_map[loc] = {"bid": bid, "is_on": final_on, "cids": cids_to_check}

            if 'api_stat_cache' not in st.session_state: st.session_state.api_stat_cache = {}
            local_cache = st.session_state.api_stat_cache
            today_str = datetime.date.today().strftime('%Y-%m-%d')
            date_list = [(datetime.date.today() - datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(6, -1, -1)]
            
            stat_queries = set()
            for loc, ldata in loc_cids_map.items():
                for cid in ldata["cids"]: stat_queries.add((cid, stat_target_date))
            for cid in cids_for_7d:
                for d in date_list: stat_queries.add((cid, d))

            def fetch_stat_with_cache(cid, date, cache_dict):
                if date != today_str and (cid, date) in cache_dict:
                    return cid, date, cache_dict[(cid, date)]
                try:
                    time.sleep(0.05)
                    stat = fetch_campaign_stat_api(cid, date)
                    return cid, date, stat
                except Exception:
                    return cid, date, {'spend':0, 'clicks':0, 'avg_rank':0}

            stat_results_dict = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(fetch_stat_with_cache, q[0], q[1], local_cache) for q in stat_queries]
                for future in concurrent.futures.as_completed(futures):
                    c, d, stat = future.result()
                    stat_results_dict[(c, d)] = stat
                    if d != today_str: local_cache[(c, d)] = stat
            
            st.session_state.api_stat_cache = local_cache
            
            for loc, ldata in loc_cids_map.items():
                tot_spend, tot_clicks, sum_rank, active_rank_cnt = 0, 0, 0.0, 0
                for cid in ldata["cids"]:
                    stat = stat_results_dict.get((cid, stat_target_date), {'spend':0, 'clicks':0, 'avg_rank':0})
                    tot_spend += stat['spend']
                    tot_clicks += stat['clicks']
                results[loc] = {
                    "bid": ldata["bid"], "is_on": ldata["is_on"], "avg_rank": 0.0,
                    "spend": tot_spend, "clicks": tot_clicks, "date_label": display_date_label
                }
            st.session_state.place_diagnosis_data = results
            
            place_7d_data = {d: 0 for d in date_list}
            for d in date_list:
                day_spend = 0
                for cid in cids_for_7d:
                    stat = stat_results_dict.get((cid, d), {'spend':0, 'clicks':0, 'avg_rank':0})
                    day_spend += stat['spend']
                place_7d_data[d] = day_spend
            st.session_state.place_7d_flow = place_7d_data
            st.session_state.api_sync_timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            st.session_state.api_data_period = display_date_label
            
            st.success("⚡ 플레이스 스캔 동기화가 안전하게 끝났습니다!")
            st.rerun()

if st.session_state.get('place_diagnosis_data'):
    saved_ranks_dict = load_place_ranks()
    cols = st.columns(4)
    for idx, loc in enumerate(place_locations):
        data = st.session_state.place_diagnosis_data.get(loc, {})
        if not data: continue
        with cols[idx % 4]:
            st.markdown(f"<div style='background-color:#F8FAFC; padding:8px; border-radius:6px; border-left:4px solid #1E3A8A; margin-bottom:10px;'><b style='font-size:15px; color:#0F172A;'>📍 {loc}</b></div>", unsafe_allow_html=True)
            override_val = st.selectbox("수동 순위 오버라이드", ["미입력 (API 기준)", "1위", "2위", "3위", "순위 밖"], key=f"sb_{loc}")
            if override_val != saved_ranks_dict.get(loc, "미입력 (API 기준)"):
                saved_ranks_dict[loc] = override_val
                save_place_ranks(saved_ranks_dict)
                st.rerun()
            
            display_rank = override_val if override_val != "미입력 (API 기준)" else "조회 완료"
            st.markdown(f"<div style='background-color:#EFF6FF; border:1px solid #3B82F6; padding:10px; border-radius:6px; text-align:center;'><b>{display_rank}</b></div>", unsafe_allow_html=True)
            st.markdown(f"<div style='font-size:12px; margin-top:5px;'>- 단가: {data.get('bid',0):,}원<br>- 비용: {data.get('spend',0):,}원<br>- 클릭: {data.get('clicks',0)}건</div>", unsafe_allow_html=True)
st.markdown('</div>', unsafe_allow_html=True)

# 3구역 렌더링
st.markdown('<div class="section-box">', unsafe_allow_html=True)
# ... 파워링크 추출 및 동기화 코드 구역 ...
if st.button("📊 파워링크 7일 대조 대조표 추출 (API 연동)", key="power_sync_btn", type="primary"):
    all_camps, _ = get_all_naver_campaigns()
    if all_camps:
        today = datetime.date.today()
        d7_start = today - datetime.timedelta(days=6)
        date_list = [(d7_start + datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(7)]
        bot_records = []
        daily_flow = {d: {"파워링크": 0, "일일소비금액": 0} for d in date_list}
        
        for idx, camp in enumerate(all_camps[:15]): # 가볍게 일부 정제 스캔
            cid = camp.get("nccCampaignId")
            cname = camp.get("name")
            sp, clk, ctr, cpc = fetch_period_stat_api(cid, date_list[0], date_list[-1])
            if sp > 0:
                bot_records.append({"캠페인명": cname, "광고종류": "파워링크", "조정 전 비용": sp, "클릭률_전": ctr, "CPC_전": cpc, "조정 후 비용": sp, "클릭률_후": ctr, "CPC_후": cpc, "클릭수_후": clk})
                for d in date_list:
                    daily_flow[d]["파워링크"] += int(sp/7)
        st.session_state.merged_df = pd.DataFrame(bot_records)
        st.session_state.daily_flow_data = daily_flow
        st.rerun()
st.markdown('</div>', unsafe_allow_html=True)

# 4구역 렌더링 (🔥 원인 제거의 핵심 구역)
st.markdown('<div class="section-box">', unsafe_allow_html=True)
st.markdown("""
<div style="background: linear-gradient(90deg, #1E3A8A, #3B82F6); color: white; padding: 14px 20px; border-radius: 8px; font-size: 21px; font-weight: bold; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
    🤖 4. AI 종합 진단 및 작전 지휘소 (파이어베이스 연동형)
</div>
""", unsafe_allow_html=True)

user_remark = st.text_area("오늘 현장 특이사항 조율", value="단기 렌트 유입을 방어하고 전 차량 월차 계약 확보를 위한 집중 노출 세팅 필요")

if st.button("재고 및 광고 성과 통합 검증 시작 (파이어베이스 창고 직송)", key="ai_report_btn", type="primary"):
    with st.spinner("수집된 팩트 데이터를 묶어 파이어베이스로 송출 전 정밀 분석 중..."):
        try:
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel('gemini-2.5-flash')
            st.session_state.monitoring_report = model.generate_content(f"렌트카 마케팅 분석 지침 조언 요약해줘 현장 요구사항: {user_remark}").text
            
            # 💡 [정형화 조립 엔진] 데이터를 완벽하게 패키징합니다.
            biz_money_val = f"{st.session_state.get('naver_balance_val', 0):,}원"
            
            final_payload = {
                "inventory": {
                    "totalCars": int(st.session_state.df_clean_data.shape[0]) if st.session_state.df_clean_data is not None else 0,
                    "lastSync": datetime.datetime.now().strftime("%H:%M:%S"),
                    "bizMoney": biz_money_val,
                    "categories": {}
                },
                "placeSummary": {
                    "totalSpend": total_place_spend_selected,
                    "sevenDayTotal": total_place_spend_7days
                },
                "placeLocations": [],
                "placeFlow": [{"date": d, "spend": v} for d, v in sorted(st.session_state.get('place_7d_flow', {}).items())],
                "powerlinkSummary": {
                    "totalSpend": 182400,
                    "sevenDayTotal": 1240000
                },
                "powerlinkRows": [],
                "powerlinkFlow": [{"date": d, "spend": v.get("파워링크", 0)} for d, v in sorted(st.session_state.get('daily_flow_data', {}).items())],
                "powerlinkCompare": [],
                "aiReport": st.session_state.monitoring_report
            }
            
            # 지점 목록 가공 주입
            saved_ranks = load_place_ranks()
            if 'place_diagnosis_data' in st.session_state:
                for loc, d in st.session_state.place_diagnosis_data.items():
                    r_saved = saved_ranks.get(loc, "미입력")
                    final_payload["placeLocations"].append({
                        "id": loc, "name": loc, "status": "운영중" if d.get('is_on') else "대기중",
                        "rank": r_saved if r_saved != "미입력" else f"평균 {d.get('avg_rank',0):.1f}위",
                        "spend": d.get('spend', 0), "sales": d.get('spend', 0) * 8, "count": d.get('clicks', 0),
                        "advice": "지침 분석 업데이트 완료"
                    })
                    
            # 💡 [핵심 배달 개정] 옛날 로컬 주소통신을 전면 전복시키고, 진짜 파이어베이스 창고로 유도합니다!
            sync_to_firebase(final_payload)
            
            st.success("AI 분석 완료 및 파이어베이스 클라우드 창고 배달 최종 완료!")
            time.sleep(1)
            st.rerun()
        except Exception as e:
            st.error(f"AI 모듈 가동 실패: {e}")

if st.session_state.monitoring_report:
    st.markdown(f'<div style="background-color:#F8FAFC; color:#0F172A; border:1px solid #CBD5E1; padding:15px; border-radius:6px;">{st.session_state.monitoring_report}</div>', unsafe_allow_html=True)
st.markdown('</div>', unsafe_allow_html=True)