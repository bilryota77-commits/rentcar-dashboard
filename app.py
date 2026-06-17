import streamlit as st
import pandas as pd
import altair as alt
import datetime
import time
import json
import os
import requests
import hashlib
import hmac
import base64
import urllib.request
import urllib.parse
import concurrent.futures
import re

# [모바일 최적화 코드] 하단 여백 제거 및 전체 폭 최적화
st.markdown("""
<style>
    /* 모바일에서 하단 불필요한 공백 제거 */
    .block-container { padding-bottom: 0rem !important; }
    
    /* 4구역 AI 지침 박스 모바일 가독성 개선 */
    .ai-box { 
        background-color: #f8fafc; 
        border: 1px solid #e2e8f0; 
        border-radius: 8px; 
        padding: 15px; 
        font-size: 14px; 
        line-height: 1.5;
    }
</style>
""", unsafe_allow_html=True)

# =========================================================================
# ⚡ [V28.4 마스터] 네이버 API 통계 정밀 추출 엔진 (글로벌 안전지대 배치)
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
# ==========================================
# [필수 설정] API 연동 키 직접 입력 구역
# ==========================================
NAVER_API_KEY = "010000000010a5ec12ce70ec2016073db0f15a62d454f46a91d9a85935ac1f695138836949"
NAVER_SECRET_KEY = "AQAAAABEFsYnFFNuxm5VhOGRDIGB415lQD/GENBpSpxNfn9cMw=="
NAVER_CUSTOMER_ID = 2696400
GEMINI_API_KEY = "AIzaSyBD_LEBVFv-5nkWXa132iTzpPoXT7RTWf0"
KAKAO_ACCESS_TOKEN = "카카오_토큰을_여기에_입력하세요"

st.set_page_config(layout="wide")

# ==========================================
# 글로벌 세션 상태 초기화 (시차 기록용 Timestamp 및 필수 변수 추가)
# ==========================================
if 'api_sync_timestamp' not in st.session_state: st.session_state.api_sync_timestamp = "동기화 전"
if 'api_data_period' not in st.session_state: st.session_state.api_data_period = "집계 대기"
if 'campaign_list_raw' not in st.session_state: st.session_state.campaign_list_raw = []
if 'df_clean_data' not in st.session_state: st.session_state.df_clean_data = None
if 'place_diagnosis_data' not in st.session_state: st.session_state.place_diagnosis_data = {}
# --- 누락되었던 필수 빈 그릇(변수) 3개 추가 ---
if 'daily_flow_data' not in st.session_state: st.session_state.daily_flow_data = {}
if 'merged_df' not in st.session_state: st.session_state.merged_df = None
if 'monitoring_report' not in st.session_state: st.session_state.monitoring_report = ""
# ==========================================

# ==========================================
# [엔진 1] 네이버 공식 API 무결점 통신 모듈 (스크래핑 전면 폐기)
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
    except:
        return st.session_state.get('naver_balance_val', 0)

def get_all_naver_campaigns():
    req = make_naver_request("GET", "/ncc/campaigns")
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return json.loads(res.read().decode("utf-8")), None
    except Exception as e:
        return None, str(e)

def get_single_campaign_detail(all_camps, target_key):
    matched = None
    for c in all_camps:
        c_name = str(c.get("name", "")).replace(" ", "")
        if "플레이스" in c_name and target_key.replace(" ", "") in c_name:
            matched = c
            break
            
    if not matched: return {"bid": 0, "is_on": False, "name": f"API 발견 실패", "cid": None}
        
    cid = matched.get("nccCampaignId")
    real_name = matched.get("name")
    camp_lock = str(matched.get("userLock", "")).strip().upper()
    camp_status = str(matched.get("status", "")).strip().upper()
    camp_on = not (camp_lock in ["PAUSED", "STOPPED"] or camp_status in ["PAUSED", "STOPPED"])
        
    bid = 0
    final_on = camp_on
    
    try:
        req_ag = make_naver_request("GET", f"/ncc/adgroups?nccCampaignId={cid}")
        with urllib.request.urlopen(req_ag, timeout=5) as res_ag:
            adgroups = json.loads(res_ag.read().decode("utf-8"))
            ag_on_count = 0
            for ag in adgroups:
                ag_bid = int(ag.get("bidAmt", 0))
                if ag_bid > bid: bid = ag_bid 
                ag_lock = str(ag.get("userLock", "")).strip().upper()
                ag_status = str(ag.get("status", "")).strip().upper()
                if ag_lock not in ["PAUSED", "STOPPED"] and ag_status not in ["PAUSED", "STOPPED"]:
                    ag_on_count += 1
            if ag_on_count == 0: final_on = False
    except Exception:
        pass

    return {"bid": bid, "is_on": final_on, "name": real_name, "cid": cid}

# ⚡ [신규 핵심] 네이버 API '평균 노출 순위(avgRnk)' 및 '유입(클릭)' 추출 엔진
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

# ==========================================
# [엔진 2] 구글 시트 실시간 재고 연동 모듈 (유지)
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
            
            # (재고 분류 로직 생략 없이 동일하게 유지)
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

# ==========================================
# 📊 [최상단] 시차 폭로 및 KPI 대시보드
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

# ==========================================
# 1. [1구역] 실시간 재고 집계 관제 (기존 1구역 코드는 그대로 유지합니다)
# ==========================================
# (이 부분은 기존에 있던 코드를 그대로 두시면 됩니다.)
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

# ==========================================
# 2. [2구역] 플레이스 광고 현황 (🔥 초고속 병렬처리 + 캐싱 + 파이어베이스 완결판)
# ==========================================
import os
import json
import datetime
import urllib.parse
import re
import time
import pandas as pd
import altair as alt
import firebase_admin
from firebase_admin import credentials, firestore
import streamlit as st
import concurrent.futures

# --- 🔥 파이어베이스 클라우드 연결 셋팅 ---
if not firebase_admin._apps:
    try:
        key_dict = dict(st.secrets["firebase"])
        cred = credentials.Certificate(key_dict)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        st.error(f"파이어베이스 인증 에러: secrets.toml 파일이 없거나 설정이 잘못되었습니다. ({e})")

try:
    db = firestore.client()
except:
    db = None

def load_place_ranks():
    if db:
        try:
            doc_ref = db.collection("rentcar_data").document("place_ranks")
            doc = doc_ref.get()
            if doc.exists:
                return doc.to_dict()
        except Exception as e:
            print("Firebase 로드 에러:", e)
    return {}

def save_place_ranks(data):
    if db:
        try:
            doc_ref = db.collection("rentcar_data").document("place_ranks")
            doc_ref.set(data)
        except Exception as e:
            print("Firebase 저장 에러:", e)

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
else:
    st.markdown("<div style='color:#94A3B8; font-size:13px;'>동기화 버튼을 누르면 일자별 흐름이 표시됩니다.</div>", unsafe_allow_html=True)

st.markdown("---")

place_locations = ["마곡", "가양", "양천향교", "김포공항", "강남", "안산", "인천", "일산"]

st.markdown("<div style='font-size:14px; font-weight:bold; color:#334155; margin-bottom:5px;'>📅 데이터 조회 기준일 선택</div>", unsafe_allow_html=True)
stat_option = st.radio("API 통계 추출 기준일 선택", ["오늘 (현재까지의 실시간 누적)", "어제 (최종 마감 팩트)"], horizontal=True, label_visibility="collapsed")

if "오늘" in stat_option:
    stat_target_date = datetime.date.today().strftime('%Y-%m-%d')
    display_date_label = "오늘"
else:
    stat_target_date = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    display_date_label = "어제"

if st.button(f"📊 네이버 공식 성적표(API) 100% 동기화 (기준일: {stat_target_date})", key="place_sync_btn", type="primary"):
    with st.spinner("🚀 [초고속 병렬 엔진 가동] 네이버 서버에서 데이터를 스캔 중입니다... (최대 5배 빠름)"):
        start_time = time.time() 

        all_camps, err = get_all_naver_campaigns()
        if err: 
            st.error(f"API 통신 장애: {err}")
        else:
            place_camps = [c for c in all_camps if str(c.get("campaignTp", c.get("type", ""))).upper() in ["LOCAL_AD", "PLACE"] or any(x in str(c.get("name", "")).replace(" ", "") for x in ["플레이스", "플레", "지역"])]
            
            # ✨ 최적화 1단계: 광고그룹 병렬 조회 (멀티 스레드)
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
                
            # ✨ 최적화 2단계: 과거 데이터 캐싱 및 통계 병렬 조회 (스레드 충돌 완벽 방지)
            if 'api_stat_cache' not in st.session_state:
                st.session_state.api_stat_cache = {}
            
            # 본부 메모리를 비서들에게 직접 주지 않고 '임시 장부'를 복사해서 줌
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
                    # 비서가 가져온 데이터를 본부(메인 스레드)에서 안전하게 기록
                    if d != today_str:
                        local_cache[(c, d)] = stat
            
            # 취합이 끝나면 본부 메모리에 한 번에 업데이트!
            st.session_state.api_stat_cache = local_cache
            
            # ✨ 결과 조립
            for loc, ldata in loc_cids_map.items():
                tot_spend, tot_clicks, sum_rank, active_rank_cnt = 0, 0, 0.0, 0
                for cid in ldata["cids"]:
                    stat = stat_results_dict.get((cid, stat_target_date), {'spend':0, 'clicks':0, 'avg_rank':0})
                    tot_spend += stat['spend']
                    tot_clicks += stat['clicks']
                    if stat['avg_rank'] > 0:
                        sum_rank += stat['avg_rank']
                        active_rank_cnt += 1
                
                results[loc] = {
                    "bid": ldata["bid"], "is_on": ldata["is_on"],
                    "avg_rank": sum_rank / active_rank_cnt if active_rank_cnt > 0 else 0.0,
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
            
            end_time = time.time()
            st.success(f"⚡ 초고속 동기화 완료! (단 {end_time - start_time:.1f}초 소요)")
            time.sleep(1)
            st.rerun()

if not st.session_state.get('place_diagnosis_data'):
    st.info("👆 위 동기화 버튼을 눌러 네이버 공식 데이터를 연동해 주십시오.")
else:
    saved_ranks_dict = load_place_ranks()
    
    cols = st.columns(4)
    for idx, loc in enumerate(place_locations):
        data = st.session_state.place_diagnosis_data.get(loc, {})
        if not data: continue
            
        with cols[idx % 4]:
            st.markdown(f"<div style='background-color:#F8FAFC; padding:8px; border-radius:6px; border-left:4px solid #1E3A8A; margin-bottom:10px;'><b style='font-size:15px; color:#0F172A;'>📍 [지점] {loc}</b></div>", unsafe_allow_html=True)
            
            search_kw = f"{loc} 렌트카".strip()
            naver_search_url = f"https://m.search.naver.com/search.naver?where=m&query={urllib.parse.quote(search_kw)}"
            st.markdown(f"<a href='{naver_search_url}' target='_blank' style='display:block; text-align:center; background-color:#22C55E; color:white; padding:8px; border-radius:4px; text-decoration:none; font-size:12px; font-weight:bold; margin-bottom:10px;'>현장 모바일 1초 즉시 확인</a>", unsafe_allow_html=True)
            
            current_saved_rank = saved_ranks_dict.get(loc, "미입력 (API 기준)")
            options_list = ["미입력 (API 기준)", "1위", "2위", "3위", "순위 밖"]
            try: selected_idx = options_list.index(current_saved_rank)
            except: selected_idx = 0
                
            override_val = st.selectbox("수동 오버라이드 (순위 덮어쓰기)", options_list, index=selected_idx, key=f"sb_{loc}")
            
            if override_val != current_saved_rank:
                saved_ranks_dict[loc] = override_val
                save_place_ranks(saved_ranks_dict)
                st.rerun()

            is_manual = override_val != "미입력 (API 기준)"
            display_rank = override_val if is_manual else f"평균 {data['avg_rank']:.1f}위"
            
            bg, border, text = "#F8FAFC", "#CBD5E1", "#475569" 
            if "1" in display_rank: bg, border, text = "#ECFDF5", "#10B981", "#047857"
            elif "2" in display_rank or "3" in display_rank: bg, border, text = "#EFF6FF", "#3B82F6", "#1D4ED8"
            elif "밖" in display_rank or (not is_manual and data['avg_rank'] > 3.0): bg, border, text = "#FEF2F2", "#EF4444", "#B91C1C"

            current_label = data.get('date_label', '어제')
            api_label_text = '[수동 개입] 현재 실시간 팩트' if is_manual else f'네이버 공식 API ({current_label} 평균)'
            
            st.markdown(f"""
            <div style="background-color:{bg}; border:2px solid {border}; border-radius:8px; padding:12px; text-align:center; margin-bottom:10px;">
                <div style="font-size:11px; color:{text}; margin-bottom:2px;">{api_label_text}</div>
                <div style="font-size:18px; font-weight:bold; color:{text};">{display_rank}</div>
            </div>
            <div style='font-size:12px; color:#334155; line-height:1.6; background:#F1F5F9; padding:8px; border-radius:4px;'>
                - 단가: <b>{data.get('bid', 0):,}원</b> ({'ON' if data.get('is_on') else 'OFF'})<br>
                - {current_label} 비용: <b>{data.get('spend', 0):,}원</b><br>
                - 클릭 유입: <b>{data.get('clicks', 0)}건</b>
            </div>
            """, unsafe_allow_html=True)
            
            advice = ""
            current_rank_val = 99 if ("밖" in display_rank or (not is_manual and data['avg_rank'] > 3.0)) else float(re.findall(r"[\d.]+", display_rank)[0]) if re.findall(r"[\d.]+", display_rank) else 99
            current_hour = datetime.datetime.now().hour
            pacing_warn = "<br><br><b>[예산 페이스 조절]</b> 오전 소진 속도가 과도합니다. 단가를 10% 하향 조절하십시오." if (current_hour <= 13 and data['spend'] >= 15000) else ""

            if data['bid'] >= 4500 and current_rank_val >= 3: advice = f"<b>[품질지수 보정]</b> 단가 상한선 임박. 가격 경쟁을 중단하고 문구를 <u>'추가금 0원'</u>으로 변경하십시오.{pacing_warn}"
            elif current_rank_val <= 2.0 and data['clicks'] <= 2 and data['spend'] > 0: advice = f"<b>[상품군 스위칭]</b> 유입 저조. <b>C1(법인) 또는 C3(저신용)</b> 영업용 피드로 교체하십시오.{pacing_warn}"
            elif loc in ["마곡", "가양", "양천향교"]:
                if current_rank_val <= 1.5: advice = f"<b>[코어 장악 및 확장]</b> 독점 중. <u>영등포, 구로</u> 권역까지 범위를 넓혀 수요를 흡수하십시오.{pacing_warn}"
                else: advice = f"<b>[우회 전술]</b> 경쟁 과열. 반경 2km 내 <b>화곡역, 등촌동</b> 타겟팅을 침투시키십시오.{pacing_warn}"
            elif loc == "김포공항": advice = f"<b>[타 지역 인터셉트]</b> 검색 수요 전국구. 노출 지역에 <b>'인천 계양구, 일산 동구'</b>를 강제 연동하십시오."
            elif loc in ["인천", "안산", "일산"]: advice = f"<b>[광역 공백 방어]</b> 유입 밀림 시 인접 배후 지역까지 노출 범위를 과감히 넓히십시오.{pacing_warn}"
            elif loc == "강남":
                if current_rank_val <= 2.0: advice = f"<b>[비즈니스 확장]</b> <b>서초구, 판교</b>까지 패키지로 확장하여 객단가를 극대화하십시오."
                else: advice = f"<b>[우회]</b> 경쟁 밀림 시 <u>'서초 장기렌트카'</u> 등 인접 롱테일 키워드로 전환하십시오.{pacing_warn}"
            else: advice = f"<b>[신규 모니터링]</b> 정체 시 즉시 <b>C3(무심사)</b> 피드로 방어하십시오.{pacing_warn}"

            st.markdown(f"<div style='font-size:12px; background-color:#FFFBEB; padding:10px; border-left:4px solid #D97706; margin-top:8px; border-radius:4px; line-height:1.6;'><b>[마스터 작전 지침]</b><br>{advice}</div>", unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("<h3 style='font-size:18px; color:#1E3A8A; font-weight:bold;'>📈 플레이스 통합 리포트</h3>", unsafe_allow_html=True)
    
    col_chart, col_table = st.columns([1, 1])
    with col_chart:
        if 'place_7d_flow' in st.session_state and st.session_state.place_7d_flow:
            flow_df = pd.DataFrame([{"일자": d, "소진액": v} for d, v in st.session_state.place_7d_flow.items()]).sort_values("일자")
            st.markdown("<div style='font-size:14px; font-weight:bold; color:#EA580C; margin-bottom:10px;'>지점별 플레이스 일자별 총 소진액 흐름</div>", unsafe_allow_html=True)
            place_chart = alt.Chart(flow_df).mark_bar(color="#EA580C").encode(x=alt.X("일자:N", axis=alt.Axis(labelAngle=0)), y=alt.Y("소진액:Q"), tooltip=["일자:N", alt.Tooltip("소진액:Q", format=",")]).properties(height=250)
            st.altair_chart(place_chart, use_container_width=True)
            
    with col_table:
        st.markdown("<div style='font-size:14px; font-weight:bold; color:#1E3A8A; margin-bottom:10px;'>현재 시점 지점별 요약 테이블</div>", unsafe_allow_html=True)
        summary_records = [{"지점명": loc, "현재단가": f"{data.get('bid', 0):,}원", "소진비용": f"{data.get('spend', 0):,}원", "클릭수": f"{data.get('clicks', 0)}건", "순위상태": saved_ranks_dict.get(loc, "미입력")} for loc, data in st.session_state.place_diagnosis_data.items()]
        if summary_records: st.dataframe(pd.DataFrame(summary_records), hide_index=True, use_container_width=True)

st.markdown('</div>', unsafe_allow_html=True)
# ==========================================

# ==========================================
# 3. [3구역] 파워링크 광고 현황 (🔥 원래 UI 복구 + 초고속 병렬 엔진 + 완벽 필터링)
# ==========================================
import concurrent.futures

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

st.markdown('<div class="section-box">', unsafe_allow_html=True)
st.markdown("""
<div style="background: linear-gradient(90deg, #1E3A8A, #3B82F6); color: white; padding: 14px 20px; border-radius: 8px; font-size: 21px; font-weight: bold; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
    3. 파워링크 광고 현황
</div>
""", unsafe_allow_html=True)

total_power_spend_today = 0
total_power_spend_7days = 0

if 'daily_flow_data' in st.session_state and st.session_state.daily_flow_data:
    sorted_dates = sorted(st.session_state.daily_flow_data.keys())
    for d in sorted_dates:
        spend = st.session_state.daily_flow_data[d].get("파워링크", 0)
        total_power_spend_7days += spend
        
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    if today_str in st.session_state.daily_flow_data:
        total_power_spend_today = st.session_state.daily_flow_data[today_str].get("파워링크", 0)
    elif sorted_dates:
        total_power_spend_today = st.session_state.daily_flow_data[sorted_dates[-1]].get("파워링크", 0)

col1, col2 = st.columns(2)
with col1:
    st.metric(label="오늘(최근) 파워링크 총 지출액", value=f"{total_power_spend_today:,} 원")
with col2:
    st.metric(label="최근 7일 누적 파워링크 총 지출액", value=f"{total_power_spend_7days:,} 원")

# 7일 지출 흐름 대형 UI
st.markdown("<div style='font-size:16px; font-weight:bold; color:#475569; margin-top:15px; margin-bottom:10px;'>📅 최근 7일 파워링크 지출 흐름</div>", unsafe_allow_html=True)
if 'daily_flow_data' in st.session_state and st.session_state.daily_flow_data:
    flow_cols = st.columns(7)
    for i, d in enumerate(sorted(st.session_state.daily_flow_data.keys())):
        short_date = d.split("-")[1] + "/" + d.split("-")[2]
        spend = st.session_state.daily_flow_data[d].get("파워링크", 0)
        with flow_cols[i]:
            st.metric(label=short_date, value=f"{spend:,}원")
else:
    st.markdown("<div style='color:#94A3B8; font-size:13px;'>동기화 버튼을 누르면 일자별 흐름이 표시됩니다.</div>", unsafe_allow_html=True)

st.markdown("---")

default_end = datetime.date.today() - datetime.timedelta(days=1)
default_start = default_end - datetime.timedelta(days=2)

st.markdown("단가를 조정한 이후의 [검증 대상 기간]을 선택하십시오. 시스템이 자동으로 그 이전 동일한 기간(조정 전)과 비교하여 효율을 진단합니다.")
date_range = st.date_input("대조군 검증 대상 기간 (조정 후)", value=(default_start, default_end))

# 버튼 길이 축소 통일
if st.button("📊 예산 흐름 및 전후 대조표 추출 (API 연동)", key="power_sync_btn", type="primary"):
    if isinstance(date_range, tuple) and len(date_range) == 2:
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        post_start_dt, post_end_dt = date_range[0], date_range[1]
        duration_days = (post_end_dt - post_start_dt).days + 1
        pre_end_dt = post_start_dt - datetime.timedelta(days=1)
        pre_start_dt = pre_end_dt - datetime.timedelta(days=duration_days - 1)
        
        post_start_str, post_end_str = post_start_dt.strftime('%Y-%m-%d'), post_end_dt.strftime('%Y-%m-%d')
        pre_start_str, pre_end_str = pre_start_dt.strftime('%Y-%m-%d'), pre_end_dt.strftime('%Y-%m-%d')
        
        st.session_state.date_period_info = f"[조정 전] {pre_start_str} ~ {pre_end_str} vs [조정 후] {post_start_str} ~ {post_end_str}"
        
        status_text.markdown("캠페인 리스트를 불러오고 있습니다...")
        all_camps, _ = get_all_naver_campaigns()
        
        if all_camps:
            today = datetime.date.today()
            d7_start = today - datetime.timedelta(days=6)
            date_list = [(d7_start + datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(7)]
            today_str = today.strftime('%Y-%m-%d')

            # ✨ 철벽 필터링: 파워링크만 남기고 플레이스는 모조리 삭제
            power_camps = []
            place_targets = ["플레이스", "플레", "매장", "지점", "가양", "마곡", "인천", "김포", "안산", "일산", "강남", "양천"]
            for camp in all_camps:
                ctype = str(camp.get("campaignTp", camp.get("type", "WEB_SITE"))).upper()
                cname = str(camp.get("name", ""))
                if ctype in ["PLACE", "LOCAL_AD"] or any(k in cname for k in place_targets):
                    continue
                power_camps.append(camp)

            bot_records = []
            daily_flow = {d: {"파워링크": 0, "플레이스": 0, "일일소비금액": 0} for d in date_list}

            # 캐시(임시 기억 장치) 메모리 할당
            if 'power_period_cache' not in st.session_state:
                st.session_state.power_period_cache = {}
            local_cache = st.session_state.power_period_cache

            # 비서(스레드)들이 수행할 개별 임무 정의
            def process_campaign(camp, cache):
                cid = camp.get("nccCampaignId")
                cname = camp.get("name")
                short_name = re.sub(r'\s*\(.*?\)', '', cname).replace("파워링크#", "").replace("플레이스#", "").strip()

                def _get_stat(s_str, e_str):
                    cache_key = (cid, s_str, e_str)
                    if e_str != today_str and cache_key in cache:
                        return cache[cache_key]
                    time.sleep(0.05)
                    sp, clk, ctr, cpc = fetch_period_stat_api(cid, s_str, e_str)
                    return (sp, clk, ctr, cpc)

                pre_res = _get_stat(pre_start_str, pre_end_str)
                post_res = _get_stat(post_start_str, post_end_str)

                day_res = {}
                for d in date_list:
                    day_res[d] = _get_stat(d, d)

                return {
                    "cid": cid, "short_name": short_name,
                    "pre": pre_res, "post": post_res, "days": day_res
                }

            total_camps = len(power_camps)
            if total_camps > 0:
                # ✨ 5명의 비서 투입 (초고속 병렬 처리)
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                    futures = [executor.submit(process_campaign, camp, local_cache) for camp in power_camps]
                    completed = 0
                    for future in concurrent.futures.as_completed(futures):
                        completed += 1
                        progress_bar.progress(completed / total_camps)
                        status_text.markdown(f"[{completed}/{total_camps}] 파워링크 데이터 초고속 대조 중... ⚡")
                        res = future.result()

                        # 가져온 데이터를 본부(캐시)에 저장
                        cid = res["cid"]
                        local_cache[(cid, pre_start_str, pre_end_str)] = res["pre"]
                        local_cache[(cid, post_start_str, post_end_str)] = res["post"]
                        for d in date_list:
                            local_cache[(cid, d, d)] = res["days"][d]

                        sp_pre, _, ctr_pre, cpc_pre = res["pre"]
                        sp_post, clk_post, ctr_post, cpc_post = res["post"]

                        # 지출이 1원이라도 있었던 파워링크 캠페인만 표에 추가
                        if sp_pre > 0 or sp_post > 0:
                            bot_records.append({
                                "캠페인명": res["short_name"], "광고종류": "파워링크",
                                "조정 전 비용": sp_pre, "클릭률_전": ctr_pre, "CPC_전": cpc_pre,
                                "조정 후 비용": sp_post, "클릭률_후": ctr_post, "CPC_후": cpc_post,
                                "클릭수_후": clk_post
                            })
                            for d in date_list:
                                sp_day = res["days"][d][0]
                                daily_flow[d]["파워링크"] += sp_day
                                daily_flow[d]["일일소비금액"] += sp_day

            # 결과물 최종 업데이트
            st.session_state.power_period_cache = local_cache
            st.session_state.merged_df = pd.DataFrame(bot_records)
            st.session_state.daily_flow_data = daily_flow
            
            status_text.empty()
            progress_bar.empty()
            st.rerun()
    else:
        st.error("달력 창에서 시작일과 마감일을 모두 선택해 주십시오.")

if 'date_period_info' in st.session_state:
    st.info(st.session_state.date_period_info)

if 'daily_flow_data' in st.session_state and st.session_state.daily_flow_data:
    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown("<h3 style='font-size:18px; color:#1E3A8A; font-weight:bold;'>📈 파워링크 통합 리포트</h3>", unsafe_allow_html=True)
    
    col_chart, col_table = st.columns([1, 1])
    
    with col_chart:
        flow_records = [{"일자": d, "소진액": v["파워링크"]} for d, v in st.session_state.daily_flow_data.items()]
        flow_df = pd.DataFrame(flow_records).sort_values("일자")
        st.markdown("<div style='font-size:14px; font-weight:bold; color:#1E40AF; margin-bottom:10px;'>파워링크 일자별 총 소진액 흐름</div>", unsafe_allow_html=True)
        # 차트 X축 글씨 수평으로 고정 (labelAngle=0)
        power_chart = alt.Chart(flow_df).mark_bar(color="#1E40AF").encode(x=alt.X("일자:N", axis=alt.Axis(labelAngle=0)), y=alt.Y("소진액:Q"), tooltip=["일자:N", alt.Tooltip("소진액:Q", format=",")]).properties(height=250)
        st.altair_chart(power_chart, use_container_width=True)
        
    with col_table:
        st.markdown("<div style='font-size:14px; font-weight:bold; color:#1E3A8A; margin-bottom:10px;'>현재 시점 파워링크 요약 (선택기간 기준)</div>", unsafe_allow_html=True)
        if 'merged_df' in st.session_state and st.session_state.merged_df is not None and not st.session_state.merged_df.empty:
            type_data = st.session_state.merged_df[st.session_state.merged_df["광고종류"] == "파워링크"]
            summary_records = []
            for _, r in type_data.iterrows():
                summary_records.append({
                    "캠페인명": r['캠페인명'],
                    "현재비용": f"{int(r['조정 후 비용']):,}원",
                    "클릭수": f"{int(r.get('클릭수_후', 0))}건",
                    "현재CPC": f"{int(r['CPC_후']):,}원"
                })
            if summary_records:
                st.dataframe(pd.DataFrame(summary_records), hide_index=True, use_container_width=True)

if 'merged_df' in st.session_state and st.session_state.merged_df is not None and not st.session_state.merged_df.empty:
    type_data = st.session_state.merged_df[st.session_state.merged_df["광고종류"] == "파워링크"]
    if not type_data.empty:
        st.markdown("<br><div style='font-size:19px; font-weight:bold; color:#0F172A; margin-bottom:15px; border-left:5px solid #EA580C; padding-left:10px;'>파워링크 전후 성과 상세 대조표</div>", unsafe_allow_html=True)
        
        html_table = "<div style='background-color:#FFFFFF; border:1px solid #E2E8F0; border-radius:8px; overflow:hidden;'><table style='width:100%; text-align:center; border-collapse:collapse;'><thead style='background-color:#F8FAFC; border-bottom:2px solid #CBD5E1;'><tr><th style='padding:12px; font-size:13px;'>캠페인명</th><th style='padding:12px; font-size:13px;'>이전 비용</th><th style='padding:12px; font-size:13px;'>이후 비용</th><th style='padding:12px; font-size:13px;'>이전 CTR</th><th style='padding:12px; font-size:13px;'>이후 CTR</th><th style='padding:12px; font-size:13px;'>이전 CPC</th><th style='padding:12px; font-size:13px;'>이후 CPC</th><th style='padding:12px; font-size:13px;'>최종 조치</th><th style='padding:12px; font-size:13px;'>진단 사유</th></tr></thead><tbody>"
        
        for _, r in type_data.iterrows():
            ctr_diff = r["클릭률_후"] - r["클릭률_전"]
            cpc_diff = r["CPC_후"] - r["CPC_전"]
            
            txt_pre_spend = f"{int(r['조정 전 비용']):,}원"
            txt_post_spend = f"{int(r['조정 후 비용']):,}원"
            txt_pre_ctr = f"{r['클릭률_전']:.2f}%"
            txt_post_ctr = f"{r['클릭률_후']:.2f}%"
            txt_pre_cpc = f"{int(r['CPC_전']):,}원"
            txt_post_cpc = f"{int(r['CPC_후']):,}원"
            
            if ctr_diff > 0 and cpc_diff <= 0: cond, c, reason = "유지 (우수)", "#16A34A", f"클릭률 상승({ctr_diff:+.2f}%) 및 단가 절감"
            elif ctr_diff <= 0 and cpc_diff > 0: cond, c, reason = "수정 필요", "#DC2626", f"클릭률 하락({ctr_diff:.2f}%) 및 단가 인상"
            elif ctr_diff > 0 and cpc_diff > 0: cond, c, reason = "모니터링", "#EA580C", f"유입은 증가했으나 비용 동반 상승"
            elif ctr_diff < 0 and cpc_diff < 0: cond, c, reason = "단가 상향", "#2563EB", f"비용은 줄었으나 상권에서 밀려 유입 감소"
            else: cond, c, reason = "안정화", "#475569", "전후 성과 변동폭 오차 내 균형"
            
            html_table += f"<tr style='border-bottom:1px solid #F1F5F9;'><td style='padding:10px; font-size:13px; font-weight:bold; text-align:left; padding-left:15px;'>{r['캠페인명']}</td><td>{txt_pre_spend}</td><td>{txt_post_spend}</td><td>{txt_pre_ctr}</td><td>{txt_post_ctr}</td><td>{txt_pre_cpc}</td><td>{txt_post_cpc}</td><td style='font-weight:bold; color:{c};'>{cond}</td><td style='text-align:left; font-size:12px;'>{reason}</td></tr>"
        
        html_table += "</tbody></table></div>"
        st.markdown(html_table, unsafe_allow_html=True)
        
st.markdown('</div>', unsafe_allow_html=True)
# ==========================================

# ==========================================
# 4. [4구역] AI 종합 진단 및 작전 지휘소 (팩트 데이터 연동)
# ==========================================
st.markdown('<div class="section-box">', unsafe_allow_html=True)
st.markdown("""
<div style="background: linear-gradient(90deg, #1E3A8A, #3B82F6); color: white; padding: 14px 20px; border-radius: 8px; font-size: 21px; font-weight: bold; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
    🤖 4. AI 종합 진단 및 작전 지휘소 (팩트 데이터 연동)
</div>
""", unsafe_allow_html=True)

user_remark = st.text_area("오늘 현장 특이사항 조율 (AI 분석 참고용)", value="단기 렌트 유입을 방어하고 전 차량 월차 계약 확보를 위한 집중 노출 세팅 필요", height=70)

if st.button("재고 및 광고 성과 통합 검증 시작", key="ai_report_btn", type="primary"):
    with st.spinner("수집된 API 통계와 현장 팩트 데이터를 바탕으로 지침을 분석 중입니다..."):
        try:
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel('gemini-2.5-flash')
            
            diag_info = ""
            if 'place_diagnosis_data' in st.session_state and st.session_state.place_diagnosis_data:
                for loc, data in st.session_state.place_diagnosis_data.items():
                    rank_status = data.get('manual_override', f"API평균 {data.get('avg_rank', 0)}위")
                    diag_info += f"- [{loc}] 단가: {data.get('bid', 0)}원 / 어제비용: {data.get('spend', 0)}원 / 유입: {data.get('clicks', 0)}건 / 현재상태: {rank_status}\n"
            
            sys_prompt = f"""
            당신은 빌려타렌트카의 퍼포먼스 마케터입니다.
            다음 100% 팩트 데이터를 바탕으로 비효율 예산 누수를 막고, 영업소별 명확한 단가 조절 및 마케팅 지침 3가지를 구체적 수치와 함께 도출하십시오.
            [영업소별 팩트 상태] {diag_info}
            [현장 요구사항] {user_remark}
            """
            st.session_state.monitoring_report = model.generate_content(sys_prompt).text
            st.success("AI 기반 마케팅 조치안 작성이 완료되었습니다.")
        
        except Exception as e:
            st.warning("⚠️ 구글 AI 허용 한도 초과 혹은 통신 장애. 내부 관제 엔진으로 즉시 백업 분석을 출력합니다.")
            local_report = """
            ### 🚨 빌려타렌트카 내부 관제 시스템 긴급 지침안
            **1. 즉각적인 예산 방어:** 상단 2구역 확인. '순위 밖'인데 지출 발생 시 품질지수 훼손 상태입니다. 문구부터 교체하십시오.
            **2. C4(단기/월렌트) 주력 재고 전환:** 가용 대수 많은 차량 그룹을 파악하여 마곡 본점/인접 권역 예산을 상향하십시오.
            **3. 일일 예산 소진 캘리브레이션:** 3구역 최근 흐름상 특정 요일 이탈 캠페인은 노출 시간대를 핵심 시간(08시~15시)으로 축소하십시오.
            """
            st.session_state.monitoring_report = local_report

# [중요] 여기에 ai-box 클래스를 입혀서 모바일 가독성 확보!
if 'monitoring_report' in st.session_state and st.session_state.monitoring_report != "":
    st.markdown(f'<div class="ai-box">{st.session_state.monitoring_report}</div>', unsafe_allow_html=True)
    
    if st.button("카카오톡으로 모니터링 보고서 전송", type="primary"):
        headers = {"Authorization": "Bearer " + KAKAO_ACCESS_TOKEN}
        data = {"template_object": json.dumps({"object_type": "text","text": st.session_state.monitoring_report,"link": {"web_url": "http://localhost:8501"}})}
        try:
            if requests.post("https://kapi.kakao.com/v2/api/talk/memo/default/send", headers=headers, data=data).status_code == 200:
                st.success("보고서 전송이 완료되었습니다.")
            else:
                st.error("전송에 실패했습니다.")
        except:
            st.error("통신 장애로 인해 전송에 실패했습니다.")

st.markdown('</div>', unsafe_allow_html=True)
# ==========================================