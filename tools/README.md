# VocabCloud 快速查單字工具

在 Mac 上任何 App **選取英文單字 → 浮動視窗顯示中文翻譯＋音標 → 一鍵加入單字庫**。

加入的單字會寫進 Google Drive 本機同步資料夾 `VocabularyLog/vocab_YYYY-MM-DD.md`，
之後在 [VocabCloud 網頁](https://addis-vocab.streamlit.app/) 按「☁️ Drive 同步」即可開始練習。

## 架構：常駐 daemon（速度關鍵）

`quick_vocab.py --serve` 是常駐背景程式：視窗預先建好藏著、字典連線備妥，
觸發時只是透過 unix socket 喚醒，**0.1 秒內出現**，且視窗能浮在全螢幕 App 之上。
daemon 沒在跑時自動退回一次性模式（較慢但可用）。

開機自動啟動（裝好 plist 後執行一次）：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vocabcloud.quickvocab.plist
```

改過 `quick_vocab.py` 之後要重啟 daemon 才生效：

```bash
launchctl kickstart -k gui/$(id -u)/com.vocabcloud.quickvocab
```

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

### 更快：用 BetterTouchTool 觸發（跳過 Automator 服務的 ~1 秒啟動）

macOS 服務機制本身每次要 ~1 秒啟動，BTT 直接執行可以壓到 ~0.2 秒：

1. BTT → Keyboard Shortcuts → 新增快捷鍵（例如 ⌃⌥V，先把服務那邊的快捷鍵移除）
2. 加入動作序列：
   - 「Send Keyboard Shortcut ⌘C」（複製選取的字）
   - 短暫 delay 0.05s
   - 「Execute Terminal Command (async)」：
     `/opt/homebrew/bin/python3 /Users/yourulyu/coding/vocabcloud/tools/quick_vocab.py "$(pbpaste)"`

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
