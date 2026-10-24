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
3. 浮動視窗顯示：音標、中文翻譯、詞性、英中釋義、例句，並自動唸一次
4. 視窗內快速鍵：**空白鍵** 重播發音、**單按 ⌘**（或 A、⏎）加入單字庫並自動關窗、
   **Esc** 關閉，**點視窗外任何地方也會自動關閉**（點回原 App 時焦點直接回去）
5. 按「➕ 加入單字庫」後到 VocabCloud 網頁按「☁️ Drive 同步」匯入（重複的字會自動擋掉）

### 聽 Podcast 的建議流程

空白鍵暫停 → 雙擊生字 → 觸發查字（下方快捷鍵）→ 看翻譯＋聽發音（空白鍵重播）→
需要就 ⏎ 加入字庫 → **直接點回 Podcast**（視窗自動關）→ 空白鍵繼續播放。

## 鍵盤快捷鍵

目前設定為 **⌃⌥V**（Control+Option+V）。
注意 ⌃⌥D 不能用——那是 macOS 內建「切換 Dock 自動隱藏」的快捷鍵，會被系統攔走。
要改：系統設定 → 鍵盤 → 鍵盤快速鍵… → 服務 → 文字 → 「VocabCloud 查單字」。

### 推薦：用 BetterTouchTool 觸發（最快，~0.2 秒）

macOS 服務機制本身每次要 ~1 秒啟動，BTT 直接執行可以壓到 ~0.2 秒。
動作序列（兩種觸發都一樣）：

- 「Send Keyboard Shortcut ⌘C」（複製選取的字）
- delay 0.05s
- 「Execute Terminal Command (async)」：
  `/opt/homebrew/bin/python3 /Users/yourulyu/coding/vocabcloud/tools/quick_vocab.py "$(pbpaste)"`

觸發方式：**四指輕點**（4 Finger Tap，設定在 BTT）。
系統四指手勢都是滑動，輕點零衝突，三指拖移照常可用。

曾試過並捨棄的方案（供參考）：
- 三指輕按/按下：BTT 的三指手勢辨識會讓系統「三指拖移」選字慢半拍（漏掉第一個字母）
- F6：內建鍵盤 F 區是媒體鍵，單按變勿擾
- ⌃⌥V 服務選單：走 Automator，每次觸發多 ~1 秒
終端機備援：`python3 tools/quick_vocab.py <word>`

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

# 3. 簡轉繁套件（OpenCC s2twp：台灣正體+台灣用語；ECDICT 原文是簡體，
#    開源界沒有同等規模的原生繁體離線英漢字典，故採轉換）
python3 -m pip install --user --break-system-packages opencc-python-reimplemented
```

注意 `.workflow` 裡的 script 路徑與 `quick_vocab.py` 裡的 `VOCAB_LOG_DIR` 寫死了本機路徑，換電腦要改。

## 資料來源

- **翻譯/音標/詞性/釋義：本機 [ECDICT](https://github.com/skywind3000/ECDICT) 離線字典（毫秒級，開窗即顯示）**，簡轉繁用 zhconv
- 例句＋真人發音 mp3：[dictionaryapi.dev](https://dictionaryapi.dev/)（背景補上，服務不穩就略過）
- 本機查不到的字/片語才走網路：[Datamuse](https://www.datamuse.com/api/) + [MyMemory](https://mymemory.translated.net/doc/spec.php)
- 發音：開窗自動唸一次（系統 TTS），按「🔊 發音」重播（有真人 mp3 就播 mp3）
