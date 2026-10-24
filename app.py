import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import json
import pandas as pd
import time
import re
from gtts import gTTS
import io
from datetime import datetime, timedelta, date

# ==========================================
# 1. 網頁初始化與全域設定
# ==========================================
# 註：本 app 不呼叫任何 AI API。單字的生成一律在本機用 Claude Code Max
#     訂閱完成（見 ~/15min/vocab_builder.py），雲端只負責複習與同步。
st.set_page_config(page_title="VocabCloud 雲端單字系統", page_icon="☁️", layout="centered")

# ==========================================
# 2. 雲端認證與連線模組
# ==========================================
@st.cache_resource
def get_gcp_credentials():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    gcp_info = json.loads(st.secrets["gcp_service_account"]["json_key"])
    return Credentials.from_service_account_info(gcp_info, scopes=scope)

def get_sheet():
    creds = get_gcp_credentials()
    client = gspread.authorize(creds)
    return client.open("VocabCloud_DB").sheet1

# ==========================================
# 3. 狀態管理 (Session State)
# ==========================================
if 'page' not in st.session_state:
    st.session_state.page = 'menu'
if 'practice_cards' not in st.session_state:
    st.session_state.practice_cards = []
if 'current_index' not in st.session_state:
    st.session_state.current_index = 0
if 'is_front' not in st.session_state:
    st.session_state.is_front = True
if 'practice_mode' not in st.session_state:
    st.session_state.practice_mode = 'flashcard' # 'flashcard' 或 'cloze'

def go_to_menu():
    st.session_state.page = 'menu'
    st.session_state.practice_cards = []
    
def play_audio(text):
    if not text: return
    try:
        tts = gTTS(text=text, lang='en')
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        st.audio(fp, format="audio/mp3", autoplay=True)
    except Exception as e:
        st.error(f"語音生成失敗: {e}")

# ==========================================
# 4. 🚀 核心升級：防彈版資料庫整理與 SRS 演算法
# ==========================================
def fetch_and_clean_data():
    """讀取資料庫，並加上『防彈機制』避免空白或格式錯誤"""
    sheet = get_sheet()
    records = sheet.get_all_records()
    df = pd.DataFrame(records)
    
    if df.empty:
        return df

    # 1. 清理標題列的無形空白 (解決不小心打成 "Level " 的問題)
    df.columns = df.columns.str.strip()

    today_str = date.today().isoformat()
    
    # 2. 確保所有必備欄位都在，沒有就補上空字串
    for col in ['Tag', 'Level', 'Next_Review', 'Created_At']:
        if col not in df.columns:
            df[col] = ''
            
    # 3. 防彈轉型 (自動把奇怪的字元轉成預設值，不怕讀到錯位的資料)
    df['Tag'] = df['Tag'].fillna('未分類').astype(str).replace(r'^\s*$', '未分類', regex=True)
    df['Level'] = pd.to_numeric(df['Level'], errors='coerce').fillna(0).astype(int)
    df['Next_Review'] = df['Next_Review'].fillna(today_str).astype(str).replace(r'^\s*$', today_str, regex=True)
    df['Created_At'] = df['Created_At'].fillna(today_str).astype(str).replace(r'^\s*$', today_str, regex=True)
    
    return df

def safe_int(val):
    """安全地將儲存格內容轉為整數"""
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return 0

def update_score_in_sheets(word, is_correct):
    """結合 SRS 演算法，並使用『動態尋找欄位』避免寫錯格"""
    try:
        sheet = get_sheet()
        # 抓取第一列標題，用來動態定位 (不怕欄位被移動或插隊)
        headers = [str(h).strip() for h in sheet.row_values(1)]
        
        # 找出真正對應欄位的行號 (Google Sheet 是從 1 開始算)
        col_correct = headers.index('Correct') + 1 if 'Correct' in headers else 7
        col_wrong = headers.index('Wrong') + 1 if 'Wrong' in headers else 8
        col_level = headers.index('Level') + 1 if 'Level' in headers else 11
        col_next = headers.index('Next_Review') + 1 if 'Next_Review' in headers else 12

        cell = sheet.find(word)
        if cell:
            row = cell.row
            curr_correct = safe_int(sheet.cell(row, col_correct).value)
            curr_wrong = safe_int(sheet.cell(row, col_wrong).value)
            curr_level = safe_int(sheet.cell(row, col_level).value)
            
            today = date.today()
            
            if is_correct:
                new_level = curr_level + 1
                days = 2 ** (new_level - 1) if new_level <= 5 else 30
                next_review = (today + timedelta(days=days)).isoformat()
                
                sheet.update_cell(row, col_correct, curr_correct + 1)
                sheet.update_cell(row, col_level, new_level)
                sheet.update_cell(row, col_next, next_review)
            else:
                sheet.update_cell(row, col_wrong, curr_wrong + 1)
                sheet.update_cell(row, col_level, 0)
                sheet.update_cell(row, col_next, today.isoformat())
    except Exception as e:
        print(f"分數與 SRS 更新失敗: {e}")

# ==========================================
# 5. 跨雲同步與 AI 擴充 (支援新欄位)
# ==========================================
def sync_cloud_drive_to_sheets():
    creds = get_gcp_credentials()
    drive_service = build('drive', 'v3', credentials=creds)
    results = drive_service.files().list(q="name contains '.md' and trashed = false", fields="files(id, name)").execute()
    files = results.get('files', [])
    if not files: return 0

    sheet = get_sheet()
    existing_words = {str(row.get("Word", "")).strip().lower() for row in sheet.get_all_records()}
    new_rows, added_count = [], 0
    today_str = date.today().isoformat()
    
    for file in files:
        content_bytes = drive_service.files().get_media(fileId=file['id']).execute()
        content = content_bytes.decode('utf-8', errors='ignore')
        for block in content.split('---'):
            if not block.strip(): continue
            try:
                word = re.search(r'###\s+(.*)', block).group(1).strip()
                if word.lower() in existing_words: continue 
                
                phonetic = re.search(r'\*\*\s*KK音標：\*\*\s*(.*)', block).group(1).strip() if re.search(r'\*\*\s*KK音標：\*\*\s*(.*)', block) else ""
                pos = re.search(r'\*\*\s*詞性：\*\*\s*(.*)', block).group(1).strip() if re.search(r'\*\*\s*詞性：\*\*\s*(.*)', block) else ""
                definition = re.search(r'\*\*\s*釋義：\*\*\s*(.*)', block).group(1).strip() if re.search(r'\*\*\s*釋義：\*\*\s*(.*)', block) else ""
                example = re.search(r'\*\*\s*例句：\*\*\s*(.*)', block).group(1).strip() if re.search(r'\*\*\s*例句：\*\*\s*(.*)', block) else ""
                translation = re.search(r'\*\*\s*翻譯：\*\*\s*(.*)', block).group(1).strip() if re.search(r'\*\*\s*翻譯：\*\*\s*(.*)', block) else ""
                
                # 新增欄位對應: Word(1)... Wrong(8), Source(9), Tag(10), Level(11), Next_Review(12), Created_At(13)
                new_rows.append([word, phonetic, pos, definition, example, translation, 0, 0, 2, "日常擷取", 0, today_str, today_str])
                existing_words.add(word.lower())
                added_count += 1
            except AttributeError:
                continue
    if new_rows: sheet.append_rows(new_rows)
    return added_count

# ==========================================
# 6. 新增單字說明頁
# ==========================================
def render_add_word():
    st.button("⬅️ 返回首頁", on_click=go_to_menu)
    st.title("✏️ 怎麼新增單字")
    st.info("單字的 AI 解釋一律在**你的 Mac** 上產生，用的是 Claude Code Max 訂閱，雲端這邊不需要任何 API Key。")

    st.subheader("方法一：查單字浮動視窗（日常用這個）")
    st.markdown(
        "在任何 App 選取英文單字 → **觸控板四指輕點**。\n\n"
        "浮動視窗會顯示**中文翻譯＋音標＋釋義**並自動發音，"
        "按「➕ 加入單字庫」（或 ⏎）就存進 Google Drive 的 `VocabularyLog`。\n\n"
        "詳見 repo 的 `tools/README.md`。"
    )

    st.subheader("方法二：終端機")
    st.code('python3 ~/coding/vocabcloud/tools/quick_vocab.py --add "resilient"', language="bash")

    st.subheader("最後一步")
    st.markdown("回首頁按 **☁️ Drive 同步**，就會把新單字讀進字庫，然後就能開始複習了。")

# ==========================================
# 7. 主頁面 (多維度練習菜單)
# ==========================================
def render_menu():
    st.title("☁️ VocabCloud 雲端單字大腦")
    
    try:
        df = fetch_and_clean_data()
        total_words = len(df)
        st.markdown(f"### 📊 雲端庫存: **{total_words}** 字")
    except Exception as e:
        st.error(f"讀取資料庫失敗：{e}")
        return

    st.write("---")
    st.subheader("🎯 智能特訓中心")
    
    if total_words == 0:
        st.warning("⚠️ 庫存為空，請先在 Mac 上用捷徑存單字，再按下方的「☁️ Drive 同步」。")
    else:
        # 1. 📅 SRS 今日複習
        today_str = date.today().isoformat()
        srs_df = df[df['Next_Review'] <= today_str]
        
        col_srs, col_new = st.columns(2)
        with col_srs:
            if st.button(f"📅 今日應複習 ({len(srs_df)} 字)", type="primary", use_container_width=True):
                if srs_df.empty:
                    st.success("太神啦！今天沒有需要複習的單字！")
                else:
                    st.session_state.practice_cards = srs_df.to_dict('records')
                    st.session_state.current_index = 0
                    st.session_state.is_front = True
                    st.session_state.practice_mode = 'flashcard'
                    st.session_state.page = 'practice'
                    st.rerun()
                    
        # 2. ⏰ 最近一週衝刺
        seven_days_ago = (date.today() - timedelta(days=7)).isoformat()
        new_df = df[df['Created_At'] >= seven_days_ago]
        with col_new:
            if st.button(f"⏰ 近一週新字 ({len(new_df)} 字)", use_container_width=True):
                if new_df.empty:
                    st.warning("最近 7 天沒有新增單字喔。")
                else:
                    st.session_state.practice_cards = new_df.to_dict('records')
                    st.session_state.current_index = 0
                    st.session_state.is_front = True
                    st.session_state.practice_mode = 'flashcard'
                    st.session_state.page = 'practice'
                    st.rerun()

        # 3. 🏷️ 專項主題與克漏字
        st.write("")
        tags = df['Tag'].unique().tolist()
        selected_tag = st.selectbox("選擇特定主題進行特訓：", tags)
        
        col_tag, col_cloze = st.columns(2)
        with col_tag:
            if st.button(f"📚 {selected_tag} 單字卡", use_container_width=True):
                tag_df = df[df['Tag'] == selected_tag]
                st.session_state.practice_cards = tag_df.to_dict('records')
                st.session_state.current_index = 0
                st.session_state.is_front = True
                st.session_state.practice_mode = 'flashcard'
                st.session_state.page = 'practice'
                st.rerun()
                
        with col_cloze:
            if st.button(f"🕳️ {selected_tag} 克漏字", use_container_width=True):
                tag_df = df[df['Tag'] == selected_tag]
                # 隨機挑選 10 題進行克漏字
                sample_size = min(10, len(tag_df))
                st.session_state.practice_cards = tag_df.sample(sample_size).to_dict('records')
                st.session_state.current_index = 0
                st.session_state.is_front = True
                st.session_state.practice_mode = 'cloze'
                st.session_state.page = 'practice'
                st.rerun()

    st.write("---")
    st.subheader("🔄 資料管線")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("☁️ Drive 同步", type="primary", use_container_width=True):
            with st.spinner("同步中..."):
                added = sync_cloud_drive_to_sheets()
            if added:
                st.success(f"✅ 新增了 {added} 個單字！")
                time.sleep(1.5)
            else:
                st.info("沒有新單字，字庫已是最新。")
                time.sleep(1.5)
            st.rerun()
    with col2:
        if st.button("✏️ 怎麼新增單字", use_container_width=True):
            st.session_state.page = 'add_word'
            st.rerun()

# ==========================================
# 8. 測驗介面 (相容 Flashcard 與 Cloze)
# ==========================================
def render_practice():
    cards = st.session_state.practice_cards
    idx = st.session_state.current_index
    mode = st.session_state.practice_mode
    
    if idx >= len(cards):
        st.balloons()
        st.success("🎉 恭喜完成本輪特訓！")
        if st.button("返回主選單", use_container_width=True): go_to_menu(); st.rerun()
        return

    card = cards[idx]
    
    # 頂部導航
    col_back, col_prog = st.columns([1, 2])
    with col_back: st.button("⬅️ 返回", on_click=go_to_menu, use_container_width=True)
    with col_prog: st.progress((idx) / len(cards)); st.caption(f"進度: {idx + 1} / {len(cards)}")
    
    # 共用 CSS
    st.markdown("""
        <style>
        .card-box { background-color: #f8f9fa; padding: 40px 20px; border-radius: 20px; box-shadow: 0 10px 20px rgba(0,0,0,0.15); text-align: center; margin: 10px 0 25px 0; border: 1px solid #e5e7eb; }
        .word-text { color: #111827; font-size: 3.2rem; font-weight: 800; line-height: 1.2; }
        .cloze-text { color: #b91c1c; font-size: 2rem; font-weight: 700; margin-bottom: 20px; line-height: 1.4; padding: 15px; background: #fee2e2; border-radius: 10px;}
        .phonetic-text { color: #6b7280; font-size: 1.3rem; margin-bottom: 20px; font-family: monospace; }
        .def-text { color: #374151; font-size: 1.3rem; margin-top: 20px; line-height: 1.5; }
        .example-text { color: #2563eb; font-size: 1.1rem; font-style: italic; margin-top: 15px; padding: 10px; background-color: #eff6ff; border-radius: 10px; }
        .trans-text { color: #059669; font-size: 1.15rem; margin-top: 15px; font-weight: 500; }
        .divider { margin: 20px 0; border-top: 2px dashed #d1d5db; }
        </style>
    """, unsafe_allow_html=True)
    
    # --- 渲染邏輯 ---
    if mode == 'flashcard':
        front_html = f"""<div class="card-box"><div class="word-text">{card['Word']}</div><div class="phonetic-text">{card['Phonetic']}</div>"""
    else: # 克漏字模式 (利用 Regex 挖空原單字，包含可能的時態變化)
        masked_ex = re.sub(rf"(?i)\b{re.escape(card['Word'][:4])}[a-z]*\b", "_________", card['Example'])
        front_html = f"""<div class="card-box"><div class="trans-text" style="font-size: 1.5rem; margin-bottom: 10px;">{card['Translation']}</div><div class="cloze-text">{masked_ex}</div>"""

    if st.session_state.is_front:
        st.markdown(front_html + "</div>", unsafe_allow_html=True)
        if mode == 'flashcard':
            if st.button("🔊 讀單字", use_container_width=True): play_audio(card['Word'])
        st.write("") 
        if st.button("👁️ 看解答", type="primary", use_container_width=True):
            st.session_state.is_front = False; st.rerun()
    else:
        back_html = front_html + f"""<div class="divider"></div>"""
        
        if mode == 'cloze':
            back_html += f"""<div class="word-text" style="font-size: 2.5rem; color: #047857;">{card['Word']}</div><div class="phonetic-text">{card['Phonetic']}</div>"""
            
        back_html += f"""<div class="def-text"><strong>[{card['POS']}]</strong> {card['Definition']}</div><div class="example-text">{card['Example']}</div>"""
        
        if mode == 'flashcard':
             back_html += f"""<div class="trans-text">{card['Translation']}</div>"""
        
        st.markdown(back_html + "</div>", unsafe_allow_html=True)
        
        col_a1, col_a2, col_a3 = st.columns(3)
        with col_a1:
            if st.button("🔊 單字", use_container_width=True): play_audio(card['Word'])
        with col_a2:
            if st.button("🔊 說明", use_container_width=True): play_audio(card['Definition'].split('/')[0].strip())
        with col_a3:
            if st.button("🔊 例句", use_container_width=True): play_audio(card['Example'])
            
        st.write("---")
        col_ans1, col_ans2 = st.columns(2)
        with col_ans1:
            if st.button("✅ 答對了 (升級)", type="primary", use_container_width=True):
                update_score_in_sheets(card['Word'], True)
                st.session_state.current_index += 1
                st.session_state.is_front = True
                st.rerun()
        with col_ans2:
            if st.button("❌ 答錯了 (重置)", use_container_width=True):
                update_score_in_sheets(card['Word'], False)
                st.session_state.current_index += 1
                st.session_state.is_front = True
                st.rerun()

# ==========================================
# 9. 路由導航
# ==========================================
if st.session_state.page == 'menu': render_menu()
elif st.session_state.page == 'practice': render_practice()
elif st.session_state.page == 'add_word': render_add_word()