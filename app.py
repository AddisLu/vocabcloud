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
from google import genai
from datetime import datetime, timedelta, date

# ==========================================
# 1. 網頁初始化與全域設定
# ==========================================
st.set_page_config(page_title="VocabCloud 雲端單字系統", page_icon="☁️", layout="centered")

API_KEY = st.secrets["GEMINI_API_KEY"]

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
# 4. 🚀 核心升級：資料庫整理與 SRS 演算法
# ==========================================
def fetch_and_clean_data():
    """讀取資料庫並自動修補舊單字的缺漏欄位"""
    sheet = get_sheet()
    records = sheet.get_all_records()
    df = pd.DataFrame(records)
    
    if df.empty:
        return df

    today_str = date.today().isoformat()
    # 自動補齊舊資料缺少的欄位
    if 'Tag' not in df.columns: df['Tag'] = "未分類"
    if 'Level' not in df.columns: df['Level'] = 0
    if 'Next_Review' not in df.columns: df['Next_Review'] = today_str
    if 'Created_At' not in df.columns: df['Created_At'] = today_str
    
    # 填補空值
    df['Tag'] = df['Tag'].replace('', '未分類')
    df['Level'] = pd.to_numeric(df['Level'].replace('', 0)).fillna(0).astype(int)
    df['Next_Review'] = df['Next_Review'].replace('', today_str)
    df['Created_At'] = df['Created_At'].replace('', today_str)
    
    return df

def update_score_in_sheets(word, is_correct):
    """結合 SRS 間隔重複演算法的分數更新"""
    try:
        sheet = get_sheet()
        cell = sheet.find(word)
        if cell:
            row = cell.row
            current_correct = int(sheet.cell(row, 7).value or 0)
            current_wrong = int(sheet.cell(row, 8).value or 0)
            current_level = int(sheet.cell(row, 11).value or 0)
            
            today = date.today()
            
            if is_correct:
                # 答對：Level 提升，下次複習時間拉長 (1天, 2天, 4天, 8天...)
                sheet.update_cell(row, 7, current_correct + 1)
                new_level = current_level + 1
                days_to_add = 2 ** (new_level - 1) if new_level <= 5 else 30
                next_review = today + timedelta(days=days_to_add)
                sheet.update_cell(row, 11, new_level)
                sheet.update_cell(row, 12, next_review.isoformat())
            else:
                # 答錯：Level 歸零，明天立刻複習
                sheet.update_cell(row, 8, current_wrong + 1)
                sheet.update_cell(row, 11, 0)
                sheet.update_cell(row, 12, today.isoformat())
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

def run_ai_import(status_placeholder, progress_bar):
    client = genai.Client(api_key=API_KEY)
    toeic_themes = ["一般商務 (合約)", "辦公室行政", "財務與金融", "採購與物料管理", "人事與人力資源"] 
    sheet = get_sheet()
    existing_words = {str(row.get("Word", "")).strip().lower() for row in sheet.get_all_records()}
    total_added, today_str = 0, date.today().isoformat()
    
    for index, theme in enumerate(toeic_themes):
        status_placeholder.info(f"正在匯入主題 ({index+1}/{len(toeic_themes)}): {theme}...")
        progress_bar.progress((index) / len(toeic_themes))
        prompt = f"請針對多益核心單字「{theme}」主題，精選10個必考單字。格式：單字||KK音標||詞性||英文釋義 / 中文釋義||簡單例句||例句翻譯"
        try:
            response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
            lines = response.text.strip().split('\n')
            new_rows = []
            for line in lines:
                cleaned_line = re.sub(r'^(?:\d+\.\s*|-\s*|\*\s*|`\s*)', '', line.strip()).replace('`', '').strip()
                if "||" not in cleaned_line: continue
                parts = cleaned_line.split('||')
                if len(parts) == 6:
                    word, phonetic, pos, defs, ex, trans = [p.strip() for p in parts]
                    if word.lower() not in existing_words:
                        new_rows.append([word, phonetic, pos, defs, ex, trans, 0, 0, 1, theme, 0, today_str, today_str])
                        existing_words.add(word.lower())
                        total_added += 1
            if new_rows: sheet.append_rows(new_rows)
            time.sleep(2)
        except Exception as e:
            st.error(f"匯入失敗: {e}")
            break
    progress_bar.progress(1.0)
    status_placeholder.success(f"✅ 成功灌入 {total_added} 個單字！")

# ==========================================
# 6. 新增單字介面 (AI 查字典)
# ==========================================
def render_add_word():
    st.button("⬅️ 返回首頁", on_click=go_to_menu)
    st.title("✨ AI 雲端查字典")
    new_word = st.text_input("請輸入單字：", placeholder="例如：implement")
    
    if st.button("🚀 生成並寫入字庫", type="primary", use_container_width=True):
        if not new_word:
            st.warning("請先輸入單字！")
            return
        with st.spinner("AI 處理中..."):
            try:
                client = genai.Client(api_key=API_KEY)
                prompt = f"查單字：「{new_word}」。格式：單字||KK音標||詞性||英文釋義 / 繁體中文釋義||簡單英文例句||例句繁體中文翻譯"
                response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
                parts = response.text.strip().replace('`', '').split('||')
                if len(parts) == 6:
                    word, phonetic, pos, definition, example, translation = [p.strip() for p in parts]
                    today_str = date.today().isoformat()
                    sheet = get_sheet()
                    sheet.append_row([word, phonetic, pos, definition, example, translation, 0, 0, 1, "手動查閱", 0, today_str, today_str])
                    st.success(f"✅ 『{word}』加入成功！")
                    st.info(f"**音標**: {phonetic}\n\n**釋義**: {definition}\n\n**例句**: {example}")
                else:
                    st.error("AI 格式錯誤。")
            except Exception as e:
                st.error(f"錯誤：{e}")

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
        st.warning("⚠️ 庫存為空，請先同步或 AI 建立題庫。")
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
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("✨ 查字典", use_container_width=True):
            st.session_state.page = 'add_word'
            st.rerun()
    with col2:
        if st.button("☁️ Drive 同步", use_container_width=True):
            with st.spinner("同步中..."):
                sync_cloud_drive_to_sheets()
                st.rerun()
    with col3:
        if st.button("🤖 擴充題庫", use_container_width=True):
            ph, bar = st.empty(), st.progress(0)
            run_ai_import(ph, bar)
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