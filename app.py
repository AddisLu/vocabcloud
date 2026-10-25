import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import json
import pandas as pd
import random
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

def start_practice(cards_df, mode='flashcard', shuffle=True, limit=0):
    """以 DataFrame 開始一輪練習；預設洗牌，避免每次都同樣的順序。"""
    cards = cards_df.to_dict('records')
    if shuffle:
        random.shuffle(cards)
    if limit:
        cards = cards[:limit]
    st.session_state.practice_cards = cards
    st.session_state.current_index = 0
    st.session_state.is_front = True
    st.session_state.practice_mode = mode
    st.session_state.page = 'practice'
    st.rerun()


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
# 精熟門檻：參考 ai-review 題庫的 SRS（連續答對升到第 3 盒即視為精熟，
# 精熟的錯題不再進錯題特訓；答錯會重置 Level，字就會回到錯題本）。
MASTER_LEVEL = 3
STATUS = [(90, "精熟 💪"), (75, "熟練 ✨"), (50, "練習中 📖"), (0, "待加強 🔥")]

def status_of(score):
    if score is None or pd.isna(score):
        return "尚未練習"
    for th, label in STATUS:
        if score >= th:
            return label
    return "待加強 🔥"

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
    for col in ['Tag', 'Level', 'Next_Review', 'Created_At', 'Correct', 'Wrong']:
        if col not in df.columns:
            df[col] = ''

    # 3. 防彈轉型 (自動把奇怪的字元轉成預設值，不怕讀到錯位的資料)
    df['Tag'] = df['Tag'].fillna('未分類').astype(str).replace(r'^\s*$', '未分類', regex=True)
    df['Level'] = pd.to_numeric(df['Level'], errors='coerce').fillna(0).astype(int)
    df['Correct'] = pd.to_numeric(df['Correct'], errors='coerce').fillna(0).astype(int)
    df['Wrong'] = pd.to_numeric(df['Wrong'], errors='coerce').fillna(0).astype(int)
    df['Next_Review'] = df['Next_Review'].fillna(today_str).astype(str).replace(r'^\s*$', today_str, regex=True)
    df['Created_At'] = df['Created_At'].fillna(today_str).astype(str).replace(r'^\s*$', today_str, regex=True)

    # 4. 衍生欄位 (參考 ai-review 題庫：精熟 = Level 達標；錯題 = 錯過且尚未精熟)
    df['Created_Date'] = df['Created_At'].str[:10]
    df['Attempts'] = df['Correct'] + df['Wrong']
    df['Accuracy'] = (df['Correct'] * 100 / df['Attempts'].where(df['Attempts'] > 0)).round()
    df['Mastered'] = df['Level'] >= MASTER_LEVEL
    df['Open_Wrong'] = (df['Wrong'] > 0) & ~df['Mastered']

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
                    start_practice(srs_df)

        # 2. ⏰ 最近一週衝刺
        seven_days_ago = (date.today() - timedelta(days=7)).isoformat()
        new_df = df[df['Created_At'] >= seven_days_ago]
        with col_new:
            if st.button(f"⏰ 近一週新字 ({len(new_df)} 字)", use_container_width=True):
                if new_df.empty:
                    st.warning("最近 7 天沒有新增單字喔。")
                else:
                    start_practice(new_df)

        # 3. ❌ 錯題特訓 (參考 ai-review：錯最多次的優先出題，已精熟的不再出現)
        wrong_df = df[df['Open_Wrong']].sort_values('Wrong', ascending=False)
        wrong2_df = wrong_df[wrong_df['Wrong'] >= 2]
        col_w1, col_w2 = st.columns(2)
        with col_w1:
            if st.button(f"❌ 錯題總複習 ({len(wrong_df)} 字)", use_container_width=True):
                if wrong_df.empty:
                    st.success("沒有待複習的錯題，太強了！")
                else:
                    start_practice(wrong_df, shuffle=False)
        with col_w2:
            if st.button(f"🔥 錯 2 次以上 ({len(wrong2_df)} 字)", use_container_width=True):
                if wrong2_df.empty:
                    st.success("沒有重複錯的單字！")
                else:
                    start_practice(wrong2_df, shuffle=False)

        # 4. 🎯 自選練習與 📊 錯題分析
        col_pick, col_ana = st.columns(2)
        with col_pick:
            if st.button("🎯 自選練習（挑日期／挑單字）", use_container_width=True):
                st.session_state.page = 'picker'
                st.rerun()
        with col_ana:
            if st.button("📊 錯題分析", use_container_width=True):
                st.session_state.page = 'analysis'
                st.rerun()

        # 5. 🏷️ 專項主題與克漏字
        st.write("")
        tags = df['Tag'].unique().tolist()
        selected_tag = st.selectbox("選擇特定主題進行特訓：", tags)

        col_tag, col_cloze = st.columns(2)
        with col_tag:
            if st.button(f"📚 {selected_tag} 單字卡", use_container_width=True):
                start_practice(df[df['Tag'] == selected_tag])

        with col_cloze:
            if st.button(f"🕳️ {selected_tag} 克漏字", use_container_width=True):
                # 隨機挑選 10 題進行克漏字
                start_practice(df[df['Tag'] == selected_tag], mode='cloze', limit=10)

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
# 8. 🎯 自選練習 (依匯入日期／主題／錯題篩選，再逐字勾選)
# ==========================================
def render_picker():
    st.button("⬅️ 返回首頁", on_click=go_to_menu)
    st.title("🎯 自選練習")

    try:
        df = fetch_and_clean_data()
    except Exception as e:
        st.error(f"讀取資料庫失敗：{e}")
        return
    if df.empty:
        st.warning("字庫是空的，先去新增單字吧！")
        return

    # --- 篩選條件 ---
    date_counts = df['Created_Date'].value_counts().sort_index(ascending=False)
    picked_dates = st.multiselect(
        "📆 匯入日期（可複選，留空 = 全部）：",
        date_counts.index.tolist(),
        format_func=lambda d: f"{d}（{date_counts[d]} 字）")

    col_f1, col_f2 = st.columns(2)
    with col_f1:
        picked_tags = st.multiselect("🏷️ 主題（留空 = 全部）：", sorted(df['Tag'].unique().tolist()))
    with col_f2:
        keyword = st.text_input("🔍 搜尋單字或翻譯：")
    only_wrong = st.checkbox("❌ 只看待複習的錯題（錯過且尚未精熟）")

    view = df
    if picked_dates:
        view = view[view['Created_Date'].isin(picked_dates)]
    if picked_tags:
        view = view[view['Tag'].isin(picked_tags)]
    if only_wrong:
        view = view[view['Open_Wrong']]
    if keyword.strip():
        kw = keyword.strip()
        view = view[view['Word'].astype(str).str.contains(kw, case=False, regex=False) |
                    view['Translation'].astype(str).str.contains(kw, case=False, regex=False)]

    st.caption(f"符合條件：{len(view)} 字。勾選想練的單字；都不勾 = 練整個篩選結果。")

    # --- 勾選清單 ---
    table = view[['Word', 'Translation', 'Tag', 'Wrong', 'Level', 'Created_Date']].copy()
    table.insert(0, '選取', False)
    edited = st.data_editor(
        table, hide_index=True, use_container_width=True, height=380,
        disabled=['Word', 'Translation', 'Tag', 'Wrong', 'Level', 'Created_Date'],
        column_config={
            '選取': st.column_config.CheckboxColumn('選取'),
            'Word': '單字', 'Translation': '翻譯', 'Tag': '主題',
            'Wrong': '錯', 'Level': '等級', 'Created_Date': '匯入日',
        })

    chosen = view.loc[edited[edited['選取']].index] if edited['選取'].any() else view
    st.write(f"**即將練習：{len(chosen)} 字**")
    col_p1, col_p2 = st.columns(2)
    with col_p1:
        if st.button("🃏 開始單字卡", type="primary", use_container_width=True):
            if chosen.empty:
                st.warning("沒有符合條件的單字。")
            else:
                start_practice(chosen)
    with col_p2:
        if st.button("🕳️ 開始克漏字", use_container_width=True):
            if chosen.empty:
                st.warning("沒有符合條件的單字。")
            else:
                start_practice(chosen, mode='cloze')

# ==========================================
# 9. 📊 錯題分析 (參考 ai-review 的學習分析：精熟率、最常錯、主題與日期弱點)
# ==========================================
def render_analysis():
    st.button("⬅️ 返回首頁", on_click=go_to_menu)
    st.title("📊 錯題分析")

    try:
        df = fetch_and_clean_data()
    except Exception as e:
        st.error(f"讀取資料庫失敗：{e}")
        return
    if df.empty:
        st.warning("字庫是空的，先去新增單字吧！")
        return

    wrong_all = df[df['Wrong'] > 0]
    open_df = df[df['Open_Wrong']].sort_values('Wrong', ascending=False)
    mastered = wrong_all[wrong_all['Mastered']]
    pct = round(100 * len(mastered) / len(wrong_all)) if len(wrong_all) else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("曾錯過的字", len(wrong_all))
    c2.metric("已精熟 ✅", len(mastered))
    c3.metric("待複習 ❌", len(open_df))
    c4.metric("錯題精熟率", f"{pct}%")
    st.progress(pct / 100)

    # 快速特訓入口
    low_acc = df[(df['Attempts'] >= 2) & (df['Accuracy'] < 50)]
    col_q1, col_q2 = st.columns(2)
    with col_q1:
        if st.button(f"❌ 練全部待複習錯題 ({len(open_df)} 字)", type="primary", use_container_width=True):
            if open_df.empty:
                st.success("沒有待複習的錯題！")
            else:
                start_practice(open_df, shuffle=False)
    with col_q2:
        if st.button(f"📉 練答對率 <50% ({len(low_acc)} 字)", use_container_width=True):
            if low_acc.empty:
                st.success("沒有答對率過低的單字！")
            else:
                start_practice(low_acc)

    # 最常錯 Top 10
    st.write("---")
    st.subheader("🔥 最常錯的單字")
    if wrong_all.empty:
        st.info("還沒有錯題紀錄，繼續保持！")
    else:
        top = wrong_all.sort_values(['Wrong', 'Correct'], ascending=[False, True]).head(10)
        show = top[['Word', 'Translation', 'Wrong', 'Correct', 'Accuracy', 'Level', 'Created_Date']].copy()
        show['Accuracy'] = show['Accuracy'].map(lambda a: '—' if pd.isna(a) else f"{int(a)}%")
        show['狀態'] = top['Mastered'].map(lambda m: '✅ 已精熟' if m else '❌ 待複習')
        show.columns = ['單字', '翻譯', '錯', '對', '答對率', '等級', '匯入日', '狀態']
        st.dataframe(show, hide_index=True, use_container_width=True)

    # 依主題
    st.write("---")
    st.subheader("🏷️ 依主題")
    rows = []
    for tag, g in df.groupby('Tag'):
        att = int(g['Attempts'].sum())
        acc = round(100 * g['Correct'].sum() / att) if att else None
        open_n = int(g['Open_Wrong'].sum())
        score = None if acc is None else max(0, min(100, acc - min(30, open_n * 5)))
        rows.append({'主題': tag, '字數': len(g), '待複習錯題': open_n,
                     '答對率': '—' if acc is None else f"{acc}%", '狀態': status_of(score)})
    rows.sort(key=lambda r: -r['待複習錯題'])
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    # 依匯入日期
    st.write("---")
    st.subheader("📆 依匯入日期")
    rows = []
    for d, g in sorted(df.groupby('Created_Date'), key=lambda kv: kv[0], reverse=True):
        att = int(g['Attempts'].sum())
        acc = round(100 * g['Correct'].sum() / att) if att else None
        rows.append({'匯入日': d, '字數': len(g), '待複習錯題': int(g['Open_Wrong'].sum()),
                     '答對率': '—' if acc is None else f"{acc}%",
                     '尚未練過': int((g['Attempts'] == 0).sum())})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption("想練某一天匯入的單字 → 「🎯 自選練習」用匯入日期篩選。")

# ==========================================
# 10. 測驗介面 (相容 Flashcard 與 Cloze)
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
# 11. 路由導航
# ==========================================
if st.session_state.page == 'menu': render_menu()
elif st.session_state.page == 'practice': render_practice()
elif st.session_state.page == 'picker': render_picker()
elif st.session_state.page == 'analysis': render_analysis()
elif st.session_state.page == 'add_word': render_add_word()