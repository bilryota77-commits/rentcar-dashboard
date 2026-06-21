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
# [필수 설정] API 연동 키 구역
# ==========================================
NAVER_API_KEY = st.secrets["NAVER_API_KEY"]
NAVER_SECRET_KEY = st.secrets["NAVER_SECRET_KEY"]
NAVER_CUSTOMER_ID = str(st.secrets["NAVER_CUSTOMER_ID"])
GEMINI_API_KEY = "AIzaSyBD_LEBVFv-5nkWXa132iTzpPoXT7RTWf0"
KAKAO_ACCESS_TOKEN = "카카오_토큰을_여기에_입력하세요"

# ==========================================
# 글로벌 세션 상태 초기화 (깜빡임 방지용 데이터 앵커)
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
# --- 파이어베이스 초기화 (Vercel 통로의 시작점) ---
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

# --- Vercel로 가는 데이터 배달 비서 ---
def sync_to_firebase(payload_data):
    if not db:
        st.error("❌ 파이어베이스가 연결되지 않아 Vercel로 데이터를 보낼 수 없습니다.")
        return
    try:
        # Vercel이 쳐다보고 있는 'rentcar_data' -> 'main_dashboard' 방에 정확히 데이터를 꽂습니다.
        db.collection("rentcar_data").document("main_dashboard").set(payload_data)
        st.success("☁️ 파이어베이스 클라우드 전송 완료! (Vercel 화면이 즉시 업데이트됩니다)")
    except Exception as e:
        st.error(f"❌ 데이터 전송 실패: {e}")

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
# ⚡ 네이버 API 통계 추출 엔진 (순위 누락 버그 & 429 한도 초과 방어 완비)
# =========================================================================
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
    except urllib.error.HTTPError as e:
        if e.code == 429: st.warning("⚠️ 비즈머니 호출 한도 초과 (자정 초기화)")
        return st.session_state.get('naver_balance_val', 0)
    except Exception:
        return st.session_state.get('naver_balance_val', 0)

def get_all_naver_campaigns():
    req = make_naver_request("GET", "/ncc/campaigns")
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return json.loads(res.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        if e.code == 429: return None, "API_LIMIT_EXCEEDED"
        return None, str(e)
    except Exception as e:
        return None, str(e)

def fetch_campaign_stat_api(camp_id, target_date):
    try:
        time_range_str = json.dumps({"since": target_date, "until": target_date})
        # 💡 avgRnk(평균 순위) 필드를 네이버에 정식으로 요청하도록 추가했습니다.
        req = make_naver_request("GET", f"/stats?idType=CAMPAIGN&id={camp_id}&fields=%5B%22clkCnt%22%2C%22impCnt%22%2C%22salesAmt%22%2C%22avgRnk%22%5D&timeRange={urllib.parse.quote(time_range_str)}")
        with urllib.request.urlopen(req, timeout=5) as res:
            res_data = json.loads(res.read().decode("utf-8"))
            data_list = res_data.get("data", [])
            if data_list:
                stat = data_list[0]
                clicks = int(stat.get("clkCnt", 0))
                imps = int(stat.get("impCnt", 0))
                spend = int(stat.get("salesAmt", 0))
                avg_rank = float(stat.get("avgRnk", 0.0))
                ctr = (clicks / imps * 100) if imps > 0 else 0.0
                return {"spend": spend, "clicks": clicks, "ctr": ctr, "avg_rank": avg_rank}
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

# ==========================================
# 📊 대시보드 메인 레이아웃 (관리자용 뷰)
# ==========================================
st.markdown("""
<div style='background-color:#1E293B; padding:15px; border-radius:8px; display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);'>
    <div style='color:#F8FAFC; font-size:22px; font-weight:bold;'>🚀 관리자 전용: 권역별 마케팅 통합 지휘소</div>
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
    st.metric(label="시스템 모드", value="Vercel 통로 가동 중")

st.markdown("---")

# ==========================================
# 1. [1구역] 실시간 재고 집계 
# ==========================================
st.markdown('<div class="section-box">', unsafe_allow_html=True)
st.markdown("""
<div style="background: linear-gradient(90deg, #1E3A8A, #3B82F6); color: white; padding: 14px 20px; border-radius: 8px; font-size: 21px; font-weight: bold; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
    🚘 1. 실시간 재고 통계 (Vercel용)
</div>
""", unsafe_allow_html=True)

my_real_sheet = "https://docs.google.com/spreadsheets/d/1cyA7UB5wCiq58z6G103qcFLpKDjMyGKiRRkmTvGWLTk/edit?gid=0#gid=0"
if st.button("재고 데이터 동기화 실행", key="sync_btn", type="primary"):
    df, err = load_smart_spreadsheet(my_real_sheet)
    if err: st.error(f"데이터 동기화 실패: {err}")
    else:
        st.success("실시간 재고 현황 정제 완료")
        st.session_state.df_clean_data = df

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

# ==========================================
# 2. [2구역] 플레이스 광고 통계
# ==========================================
st.markdown('<div class="section-box">', unsafe_allow_html=True)
st.markdown("""
<div style="background: linear-gradient(90deg, #1E3A8A, #3B82F6); color: white; padding: 14px 20px; border-radius: 8px; font-size: 21px; font-weight: bold; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
    🎯 2. 플레이스 광고 현황 (Vercel용)
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

place_locations = ["마곡", "가양", "양천향교", "김포공항", "강남", "안산", "인천", "일산"]
stat_option = st.radio("API 통계 추출 기준일 선택", ["오늘 (현재까지의 실시간 누적)", "어제 (최종 마감 팩트)"], horizontal=True, label_visibility="collapsed")

if "오늘" in stat_option:
    stat_target_date = datetime.date.today().strftime('%Y-%m-%d')
    display_date_label = "오늘"
else:
    stat_target_date = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    display_date_label = "어제"

if st.button(f"📊 네이버 API 통계 수집 (기준일: {stat_target_date})", key="place_sync_btn", type="primary"):
    with st.spinner("🚀 네이버 서버에서 데이터를 스캔 중입니다..."):
        all_camps, err = get_all_naver_campaigns()
        if err == "API_LIMIT_EXCEEDED":
            st.error("🚨 네이버 검색광고 API 호출 한도가 초과되었습니다. 자정(00:00) 이후에 다시 시도해주세요.")
        elif err: 
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
                
            today_str = datetime.date.today().strftime('%Y-%m-%d')
            date_list = [(datetime.date.today() - datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(6, -1, -1)]
            
            stat_queries = set()
            for loc, ldata in loc_cids_map.items():
                for cid in ldata["cids"]: stat_queries.add((cid, stat_target_date))
            for cid in cids_for_7d:
                for d in date_list: stat_queries.add((cid, d))

            def fetch_stat_with_cache(cid, date):
                try:
                    time.sleep(0.02)
                    return cid, date, fetch_campaign_stat_api(cid, date)
                except Exception:
                    return cid, date, {'spend':0, 'clicks':0, 'avg_rank':0}

            stat_results_dict = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                for future in concurrent.futures.as_completed([executor.submit(fetch_stat_with_cache, q[0], q[1]) for q in stat_queries]):
                    c, d, stat = future.result()
                    stat_results_dict[(c, d)] = stat
            
            for loc, ldata in loc_cids_map.items():
                tot_spend, tot_clicks, sum_rank, active_rank_cnt = 0, 0, 0.0, 0
                for cid in ldata["cids"]:
                    stat = stat_results_dict.get((cid, stat_target_date), {'spend':0, 'clicks':0, 'avg_rank':0.0})
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
                    stat = stat_results_dict.get((cid, d), {'spend':0, 'clicks':0, 'avg_rank':0.0})
                    day_spend += stat['spend']
                place_7d_data[d] = day_spend
                
            st.session_state.place_7d_flow = place_7d_data
            st.session_state.api_sync_timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            st.success("⚡ 동기화 완료! (화면에 곧 반영됩니다)")

if not st.session_state.get('place_diagnosis_data'):
    st.info("👆 위 동기화 버튼을 눌러 네이버 공식 데이터를 연동해 주십시오.")
else:
    saved_ranks_dict = load_place_ranks()
    cols = st.columns(4)
    for idx, loc in enumerate(place_locations):
        data = st.session_state.place_diagnosis_data.get(loc, {})
        if not data: continue
            
        with cols[idx % 4]:
            st.markdown(f"<div style='background-color:#F8FAFC; padding:8px; border-radius:6px; border-left:4px solid #1E3A8A; margin-bottom:10px;'><b style='font-size:15px; color:#0F172A;'>📍 {loc}</b></div>", unsafe_allow_html=True)
            
            current_saved_rank = saved_ranks_dict.get(loc, "미입력 (API 기준)")
            options_list = ["미입력 (API 기준)", "1위", "2위", "3위", "순위 밖"]
            
            # 💡 [버그 완전 해결] 선택 상자 조작 시 화면이 무한 깜빡이던 문제를 완전히 차단했습니다.
            if f"sb_{loc}" not in st.session_state:
                st.session_state[f"sb_{loc}"] = current_saved_rank
                
            def on_rank_change(location=loc):
                new_val = st.session_state[f"sb_{location}"]
                saved_ranks_dict[location] = new_val
                save_place_ranks(saved_ranks_dict)
                
            override_val = st.selectbox("순위 덮어쓰기 (Vercel에 즉시 반영)", options_list, key=f"sb_{loc}", on_change=on_rank_change)

            is_manual = override_val != "미입력 (API 기준)"
            display_rank = override_val if is_manual else f"평균 {data['avg_rank']:.1f}위"
            
            bg, border, text = "#F8FAFC", "#CBD5E1", "#475569" 
            if "1" in display_rank: bg, border, text = "#ECFDF5", "#10B981", "#047857"
            elif "2" in display_rank or "3" in display_rank: bg, border, text = "#EFF6FF", "#3B82F6", "#1D4ED8"
            elif "밖" in display_rank or (not is_manual and data['avg_rank'] > 3.0): bg, border, text = "#FEF2F2", "#EF4444", "#B91C1C"

            st.markdown(f"""
            <div style="background-color:{bg}; border:2px solid {border}; border-radius:8px; padding:12px; text-align:center; margin-bottom:10px;">
                <div style="font-size:18px; font-weight:bold; color:{text};">{display_rank}</div>
            </div>
            <div style='font-size:12px; color:#334155; line-height:1.6; background:#F1F5F9; padding:8px; border-radius:4px;'>
                - 단가: <b>{data.get('bid', 0):,}원</b><br>
                - 비용: <b>{data.get('spend', 0):,}원</b><br>
                - 유입: <b>{data.get('clicks', 0)}건</b>
            </div>
            """, unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)

st.markdown('</div>', unsafe_allow_html=True)

# ==========================================
# 3. [3구역] 파워링크 광고 현황
# ==========================================
st.markdown('<div class="section-box">', unsafe_allow_html=True)
st.markdown("""
<div style="background: linear-gradient(90deg, #1E3A8A, #3B82F6); color: white; padding: 14px 20px; border-radius: 8px; font-size: 21px; font-weight: bold; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
    3. 파워링크 광고 데이터 수집
</div>
""", unsafe_allow_html=True)

total_power_spend_today = 0
total_power_spend_7days = 0

if st.session_state.daily_flow_data:
    sorted_dates = sorted(st.session_state.daily_flow_data.keys())
    for d in sorted_dates:
        spend = st.session_state.daily_flow_data[d].get("파워링크", 0)
        total_power_spend_7days += spend
        
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    if today_str in st.session_state.daily_flow_data:
        total_power_spend_today = st.session_state.daily_flow_data[today_str].get("파워링크", 0)

col1, col2 = st.columns(2)
with col1:
    st.metric(label="오늘(최근) 파워링크 총 지출액", value=f"{total_power_spend_today:,} 원")
with col2:
    st.metric(label="최근 7일 누적 파워링크 총 지출액", value=f"{total_power_spend_7days:,} 원")

default_end = datetime.date.today() - datetime.timedelta(days=1)
default_start = default_end - datetime.timedelta(days=2)
date_range = st.date_input("대조군 검증 대상 기간 선택", value=(default_start, default_end))

if st.button("📊 파워링크 통계 추출", key="power_sync_btn", type="primary"):
    if isinstance(date_range, tuple) and len(date_range) == 2:
        with st.spinner("파워링크 데이터를 스캔합니다..."):
            post_start_dt, post_end_dt = date_range[0], date_range[1]
            duration_days = (post_end_dt - post_start_dt).days + 1
            pre_end_dt = post_start_dt - datetime.timedelta(days=1)
            pre_start_dt = pre_end_dt - datetime.timedelta(days=duration_days - 1)
            
            post_start_str, post_end_str = post_start_dt.strftime('%Y-%m-%d'), post_end_dt.strftime('%Y-%m-%d')
            pre_start_str, pre_end_str = pre_start_dt.strftime('%Y-%m-%d'), pre_end_dt.strftime('%Y-%m-%d')
            
            all_camps, err = get_all_naver_campaigns()
            
            if err == "API_LIMIT_EXCEEDED":
                st.error("🚨 API 호출 한도가 초과되었습니다.")
            elif all_camps:
                today = datetime.date.today()
                d7_start = today - datetime.timedelta(days=6)
                date_list = [(d7_start + datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(7)]

                power_camps = []
                place_targets = ["플레이스", "플레", "매장", "지점", "가양", "마곡", "인천", "김포", "안산", "일산", "강남", "양천"]
                for camp in all_camps:
                    ctype = str(camp.get("campaignTp", camp.get("type", "WEB_SITE"))).upper()
                    cname = str(camp.get("name", ""))
                    if ctype in ["PLACE", "LOCAL_AD"] or any(k in cname for k in place_targets): continue
                    power_camps.append(camp)

                bot_records = []
                daily_flow = {d: {"파워링크": 0, "일일소비금액": 0} for d in date_list}

                def process_campaign(camp):
                    cid = camp.get("nccCampaignId")
                    cname = camp.get("name")
                    short_name = re.sub(r'\s*\(.*?\)', '', cname).replace("파워링크#", "").strip()

                    pre_res = fetch_period_stat_api(cid, pre_start_str, pre_end_str)
                    post_res = fetch_period_stat_api(cid, post_start_str, post_end_str)

                    day_res = {}
                    for d in date_list:
                        day_res[d] = fetch_period_stat_api(cid, d, d)

                    return {"cid": cid, "short_name": short_name, "pre": pre_res, "post": post_res, "days": day_res}

                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                    for future in concurrent.futures.as_completed([executor.submit(process_campaign, c) for c in power_camps]):
                        res = future.result()
                        sp_pre, _, ctr_pre, cpc_pre = res["pre"]
                        sp_post, clk_post, ctr_post, cpc_post = res["post"]

                        if sp_pre > 0 or sp_post > 0:
                            bot_records.append({
                                "캠페인명": res["short_name"], "광고종류": "파워링크",
                                "조정 전 비용": sp_pre, "클릭률_전": ctr_pre, "CPC_전": cpc_pre,
                                "조정 후 비용": sp_post, "클릭률_후": ctr_post, "CPC_후": cpc_post,
                                "클릭수_후": clk_post
                            })
                            for d in date_list:
                                daily_flow[d]["파워링크"] += res["days"][d][0]

                st.session_state.merged_df = pd.DataFrame(bot_records)
                st.session_state.daily_flow_data = daily_flow
                st.success("✅ 파워링크 스캔 성공!")

if st.session_state.merged_df is not None and not st.session_state.merged_df.empty:
    st.markdown("<div style='font-size:14px; font-weight:bold; color:#1E3A8A; margin-bottom:10px;'>파워링크 요약</div>", unsafe_allow_html=True)
    st.dataframe(st.session_state.merged_df[["캠페인명", "조정 후 비용", "클릭수_후"]].rename(columns={"조정 후 비용": "비용", "클릭수_후": "클릭"}), hide_index=True)

st.markdown('</div>', unsafe_allow_html=True)

# ==========================================
# 4. [4구역] Vercel 파이어베이스 직송 시스템
# ==========================================
st.markdown('<div class="section-box">', unsafe_allow_html=True)
st.markdown("""
<div style="background: linear-gradient(90deg, #10B981, #059669); color: white; padding: 14px 20px; border-radius: 8px; font-size: 21px; font-weight: bold; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
    🚀 4. 최종 데이터 Vercel (Next.js) 송출
</div>
""", unsafe_allow_html=True)

st.markdown("수집된 데이터를 하나의 패키지로 묶어 파이어베이스로 보냅니다. Vercel 화면에 이 데이터가 그대로 표시됩니다.")

if st.button("🌟 모든 데이터 모아서 Vercel로 보내기", key="ai_report_btn", type="primary"):
    with st.spinner("파이어베이스로 통로를 열어 데이터를 발송하는 중..."):
        biz_money_val = f"{st.session_state.get('naver_balance_val', 0):,}원"
        
        payload = {
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
                "totalSpend": total_power_spend_today,
                "sevenDayTotal": total_power_spend_7days
            },
            "powerlinkRows": [],
            "powerlinkFlow": [{"date": d, "spend": v.get("파워링크", 0)} for d, v in sorted(st.session_state.get('daily_flow_data', {}).items())],
            "powerlinkCompare": [],
            "aiReport": "Vercel 대시보드 통신 연동 완료. 데이터 정상 반영."
        }
        
        if st.session_state.df_clean_data is not None:
            df = st.session_state.df_clean_data
            target_col = [col for col in ['차급', '차종', '분류', '구분', '차량구분'] if col in df.columns]
            if target_col:
                payload["inventory"]["categories"] = {str(k): int(v) for k, v in df[target_col[0]].value_counts().to_dict().items()}
                
        if st.session_state.place_diagnosis_data:
            saved_ranks = load_place_ranks()
            for loc, d in st.session_state.place_diagnosis_data.items():
                r_saved = saved_ranks.get(loc, "미입력")
                payload["placeLocations"].append({
                    "id": loc, "name": loc, "status": "운영중" if d.get('is_on') else "대기중",
                    "rank": r_saved if r_saved != "미입력" else f"평균 {d.get('avg_rank',0):.1f}위",
                    "spend": d.get('spend', 0), "sales": d.get('spend', 0) * 8, "count": d.get('clicks', 0),
                    "advice": "정상 구동 중"
                })

        if st.session_state.merged_df is not None and not st.session_state.merged_df.empty:
            p_data = st.session_state.merged_df[st.session_state.merged_df["광고종류"] == "파워링크"]
            for idx, r in p_data.iterrows():
                payload["powerlinkRows"].append({
                    "id": str(idx), "keyword": r.get('캠페인명', ''), "status": "운영중", "rank": 0, 
                    "bid": r.get('CPC_후', 0), "spend": r.get('조정 후 비용', 0), "clicks": r.get('클릭수_후', 0), "action": "keep"
                })

        # ✨ 여기가 파이어베이스로 쏘는 진짜 통로입니다!
        sync_to_firebase(payload)

st.markdown('</div>', unsafe_allow_html=True)