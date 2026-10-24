# VocabCloud 快速查單字工具

在 Mac 上任何 App **選取英文單字 → 浮動視窗顯示中文翻譯＋音標 → 一鍵加入單字庫**。

加入的單字會寫進 Google Drive 本機同步資料夾 `VocabularyLog/vocab_YYYY-MM-DD.md`，
之後在 [VocabCloud 網頁](https://addis-vocab.streamlit.app/) 按「☁️ Drive 同步」即可開始練習。

## 使用方式

1. 在任何 App（瀏覽器、PDF、Notes…）選取英文單字
2. 右鍵 → **服務** → **VocabCloud 查單字**（或用下面設定的快捷鍵）
3. 浮動視窗顯示：音標、中文翻譯、詞性、英中釋義、例句
4. 按「➕ 加入單字庫」→ 跳通知確認已存檔（重複的字會自動擋掉）
5. 之後到 VocabCloud 網頁按「☁️ Drive 同步」匯入

## 鍵盤快捷鍵

目前設定為 **⌃⌥V**（Control+Option+V）。
注意 ⌃⌥D 不能用——那是 macOS 內建「切換 Dock 自動隱藏」的快捷鍵，會被系統攔走。
要改：系統設定 → 鍵盤 → 鍵盤快速鍵… → 服務 → 文字 → 「VocabCloud 查單字」。

## 終端機用法

```bash
python3 tools/quick_vocab.py resilient          # 跳浮動視窗
python3 tools/quick_vocab.py --add resilient    # 直接加入單字庫
python3 tools/quick_vocab.py --lookup resilient # 只印查詢結果 (JSON)
```

## 安裝（換新電腦時）

```bash
# 1. 快速動作
cp -R "tools/VocabCloud 查單字.workflow" ~/Library/Services/
/System/Library/CoreServices/pbs -update

# 2. 離線字典 ECDICT（毫秒級查詢的關鍵，812MB，不進 git）
curl -L -o /tmp/ecdict.zip "https://github.com/skywind3000/ECDICT/releases/download/1.0.28/ecdict-sqlite-28.zip"
unzip /tmp/ecdict.zip -d /tmp/ecdict && mv /tmp/ecdict/stardict.db tools/data/ecdict.db

# 3. 簡轉繁套件
python3 -m pip install --user --break-system-packages zhconv
```

注意 `.workflow` 裡的 script 路徑與 `quick_vocab.py` 裡的 `VOCAB_LOG_DIR` 寫死了本機路徑，換電腦要改。

## 資料來源

- **翻譯/音標/詞性/釋義：本機 [ECDICT](https://github.com/skywind3000/ECDICT) 離線字典（毫秒級，開窗即顯示）**，簡轉繁用 zhconv
- 例句＋真人發音 mp3：[dictionaryapi.dev](https://dictionaryapi.dev/)（背景補上，服務不穩就略過）
- 本機查不到的字/片語才走網路：[Datamuse](https://www.datamuse.com/api/) + [MyMemory](https://mymemory.translated.net/doc/spec.php)
- 發音：開窗自動唸一次（系統 TTS），按「🔊 發音」重播（有真人 mp3 就播 mp3）
