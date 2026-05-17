import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build # 新增：驅動雲端硬碟 API
import json
import pandas as pd
import time
import re
from gtts import gTTS
import io
from google import genai

# ==========================================
# 1. 網頁初始化與全域設定
# ==========================================
st.set_page_config(page_title="VocabCloud 雲端單字系統", page_icon="☁️", layout="centered")

API_KEY = "AIzaSyC6OeI7Xwsguyb4OpRtWiX2V0Fk-aRDXi4"

# ==========================================
# 2. 雲端認證與連線模組 (Google Sheets & Drive)
# ==========================================
@st.cache_resource
def get_gcp_credentials():
    """取得統一的 GCP 服務帳號憑證"""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    gcp_json_str = st.secrets["gcp_service_account"]["json_key"]
    gcp_info = json.loads(gcp_json_str)
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

def go_to_menu():
    st.session_state.page = 'menu'
    st.session_state.practice_cards = []
    
# ==========================================
# 4. 跨平台網頁語音發聲模組
# ==========================================
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
# 5. 🚀 升級：全雲端 Google Drive 同步管線
# ==========================================
def sync_cloud_drive_to_sheets():
    """直接透過 Google Drive API 讀取共用資料夾內的所有 Markdown 單字檔"""
    creds = get_gcp_credentials()
    # 建立雲端硬碟服務連結
    drive_service = build('drive', 'v3', credentials=creds)
    
    # 搜尋服務帳號有權限讀取、且檔名包含 .md 的所有未刪除檔案
    query = "name contains '.md' and trashed = false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get('files', [])
    
    if not files:
        return 0

    sheet = get_sheet()
    existing_records = sheet.get_all_records()
    existing_words = {str(row.get("Word", "")).strip().lower() for row in existing_records}
    
    added_count = 0
    new_rows = []
    
    for file in files:
        file_id = file['id']
        # 透過 API 跨雲端下載檔案文字內容
        content_bytes = drive_service.files().get_media(fileId=file_id).execute()
        content = content_bytes.decode('utf-8', errors='ignore')
        
        blocks = content.split('---')
        for block in blocks:
            if not block.strip(): continue
            try:
                word = re.search(r'###\s+(.*)', block).group(1).strip()
                if word.lower() in existing_words: continue 
                
                phonetic = re.search(r'\*\*\s*KK音標：\*\*\s*(.*)', block).group(1).strip() if re.search(r'\*\*\s*KK音標：\*\*\s*(.*)', block) else ""
                pos = re.search(r'\*\*\s*詞性：\*\*\s*(.*)', block).group(1).strip() if re.search(r'\*\*\s*詞性：\*\*\s*(.*)', block) else ""
                definition = re.search(r'\*\*\s*釋義：\*\*\s*(.*)', block).group(1).strip() if re.search(r'\*\*\s*釋義：\*\*\s*(.*)', block) else ""
                example = re.search(r'\*\*\s*例句：\*\*\s*(.*)', block).group(1).strip() if re.search(r'\*\*\s*例句：\*\*\s*(.*)', block) else ""
                translation = re.search(r'\*\*\s*翻譯：\*\*\s*(.*)', block).group(1).strip() if re.search(r'\*\*\s*翻譯：\*\*\s*(.*)', block) else ""
                
                new_rows.append([word, phonetic, pos, definition, example, translation, 0, 0, 2])
                existing_words.add(word.lower())
                added_count += 1
            except AttributeError:
                continue
    
    if new_rows:
        sheet.append_rows(new_rows)
    return added_count

def run_ai_import(status_placeholder, progress_bar):
    client = genai.Client(api_key=API_KEY)
    toeic_themes = [
        "一般商務 (合約、談判、行銷)", "辦公室行政 (會議、內部溝通)",
        "財務與金融 (銀行、投資、稅務)", "採購與物料管理 (訂購、航運)",
        "人事與人力資源 (招聘、薪酬)"
    ] 
    
    sheet = get_sheet()
    existing_records = sheet.get_all_records()
    existing_words = {str(row.get("Word", "")).strip().lower() for row in existing_records}
    
    total_added = 0
    for index, theme in enumerate(toeic_themes):
        status_placeholder.info(f"正在強力導入主題 ({index+1}/{len(toeic_themes)}): {theme}...")
        progress_bar.progress((index) / len(toeic_themes))
        
        prompt = f"""
        請針對多益（TOEIC）核心單字中的「{theme}」主題，精選 20 個目標設定在多益 600 分必考的核心單字。
        請務必使用「簡單、常用且精簡」的文字來寫英文釋義與例句。
        請嚴格依照以下格式輸出，不要有 Markdown 標記，每一行就是一個單字：
        單字||KK音標||詞性||英文釋義 / 繁體中文釋義||簡單英文例句||例句繁體中文翻譯
        """
        
        try:
            response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
            lines = response.text.strip().split('\n')
            
            new_rows = []
            for line in lines:
                cleaned_line = re.sub(r'^(?:\d+\.\s*|-\s*|\*\s*|`\s*)', '', line.strip()).replace('`', '').strip()
                if "||" not in cleaned_line: continue
                
                parts = cleaned_line.split('||')
                if len(parts) == 6:
                    word, phonetic, pos, definition, example, translation = [p.strip() for p in parts]
                    if word.lower() not in existing_words:
                        new_rows.append([word, phonetic, pos, definition, example, translation, 0, 0, 1])
                        existing_words.add(word.lower())
                        total_added += 1
            
            if new_rows:
                sheet.append_rows(new_rows)
            time.sleep(2)
            
        except Exception as e:
            st.error(f"主題 {theme} 匯入失敗: {e}")
            break
            
    progress_bar.progress(1.0)
    status_placeholder.success(f"✅ 大功告成！本次為您成功灌入 {total_added} 個核心單字！")

# ==========================================
# 6. 主頁面介面 (Main Menu)
# ==========================================
def render_menu():
    st.title("☁️ VocabCloud 雲端單字系統")
    
    try:
        sheet = get_sheet()
        records = sheet.get_all_records()
        total_words = len(records)
        st.markdown(f"### 📊 目前雲端字庫總量: **{total_words}** 字")
    except Exception as e:
        st.error(f"無法讀取資料庫，請檢查連線。系統回報錯誤：{e}")
        return

    st.write("---")
    st.subheader("🔄 資料同步機制")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("☁️ 從 Google Drive 同步日常單字", use_container_width=True):
            with st.spinner("正在跨雲端安全檢索您的 Google Drive 檔案..."):
                count = sync_cloud_drive_to_sheets()
                st.success(f"成功將雲端硬碟中的 {count} 個日常單字同步至資料庫！")
                time.sleep(1.5)
                st.rerun()
                
    with col2:
        if st.button("🤖 建立多益核心題庫 (AI 生成)", use_container_width=True):
            status_ph = st.empty()
            prog_bar = st.progress(0)
            run_ai_import(status_ph, prog_bar)
            time.sleep(2)
            st.rerun()

    st.write("---")
    st.subheader("🎯 開始練習")
    
    if total_words == 0:
        st.warning("⚠️ 你的雲端資料庫目前沒有單字，請先點擊上方的同步或 AI 生成按鈕。")
    else:
        col3, col4 = st.columns(2)
        df = pd.DataFrame(records)
        
        with col3:
            if st.button("🎲 綜合複習 (隨機 10 題)", use_container_width=True):
                sample_size = min(10, len(df))
                st.session_state.practice_cards = df.sample(sample_size).to_dict('records')
                st.session_state.current_index = 0
                st.session_state.is_front = True
                st.session_state.page = 'practice'
                st.rerun()
                
        with col4:
            if st.button("🚨 錯題特訓 (針對常錯單字)", use_container_width=True):
                mistakes_df = df[df['Wrong_Count'] > 0]
                if mistakes_df.empty:
                    st.success("太棒了！你目前沒有任何錯題紀錄！")
                else:
                    sample_size = min(10, len(mistakes_df))
                    st.session_state.practice_cards = mistakes_df.sort_values(by='Wrong_Count', ascending=False).head(sample_size).to_dict('records')
                    st.session_state.current_index = 0
                    st.session_state.is_front = True
                    st.session_state.page = 'practice'
                    st.rerun()

# ==========================================
# 7. 測驗介面 (Practice UI)
# ==========================================
def render_practice():
    cards = st.session_state.practice_cards
    idx = st.session_state.current_index
    
    if idx >= len(cards):
        st.balloons()
        st.success("🎉 太棒了！你已經完成這組單字的複習！")
        if st.button("返回主選單"):
            go_to_menu()
            st.rerun()
        return

    card = cards[idx]
    
    st.button("⬅️ 中斷並返回首頁", on_click=go_to_menu)
    st.progress((idx) / len(cards))
    st.write(f"**進度:** {idx + 1} / {len(cards)}")
    
    st.markdown("""
        <style>
        .flashcard {
            background-color: white;
            padding: 30px;
            border-radius: 15px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.1);
            text-align: center;
            margin-bottom: 20px;
        }
        </style>
    """, unsafe_allow_html=True)
    
    with st.container():
        st.markdown('<div class="flashcard">', unsafe_allow_html=True)
        st.markdown(f"<h1 style='color: #002147; font-size: 3rem;'>{card['Word']}</h1>", unsafe_allow_html=True)
        st.markdown(f"<p style='color: gray; font-size: 1.2rem;'>{card['Phonetic']}</p>", unsafe_allow_html=True)
        
        if st.button("🔊 讀單字", key=f"speak_word_{idx}"):
            play_audio(card['Word'])
        
        if not st.session_state.is_front:
            st.markdown("---")
            st.markdown(f"<p style='font-size: 1.2rem;'><strong>[{card['POS']}]</strong> {card['Definition']}</p>", unsafe_allow_html=True)
            st.markdown(f"<p style='color: #1f77b4; font-size: 1.1rem; font-style: italic;'>{card['Example']}</p>", unsafe_allow_html=True)
            
            if st.button("🔊 讀例句", key=f"speak_ex_{idx}"):
                play_audio(card['Example'])
                
            st.markdown(f"<p style='color: #2ca02c; font-size: 1.1rem;'>{card['Translation']}</p>", unsafe_allow_html=True)
            
        st.markdown('</div>', unsafe_allow_html=True)
    
    if st.session_state.is_front:
        if st.button("👁️ 翻轉卡片看解答", use_container_width=True):
            st.session_state.is_front = False
            st.rerun()
    else:
        col1, col2 = st.columns(2)
        with col1:
            if st.button("✅ 記得了", type="primary", use_container_width=True):
                update_score_in_sheets(card['Word'], is_correct=True)
                st.session_state.current_index += 1
                st.session_state.is_front = True
                st.rerun()
        with col2:
            if st.button("❌ 答錯了", type="secondary", use_container_width=True):
                update_score_in_sheets(card['Word'], is_correct=False)
                st.session_state.current_index += 1
                st.session_state.is_front = True
                st.rerun()

def update_score_in_sheets(word, is_correct):
    try:
        sheet = get_sheet()
        cell = sheet.find(word)
        if cell:
            row = cell.row
            if is_correct:
                current_val = int(sheet.cell(row, 7).value or 0)
                sheet.update_cell(row, 7, current_val + 1)
            else:
                current_val = int(sheet.cell(row, 8).value or 0)
                sheet.update_cell(row, 8, current_val + 1)
    except Exception as e:
        print(f"分數更新失敗: {e}")

# ==========================================
# 8. 路由導航
# ==========================================
if st.session_state.page == 'menu':
    render_menu()
elif st.session_state.page == 'practice':
    render_practice()