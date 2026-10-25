# VocabCloud 快速查單字工具

在 Mac 上任何 App **選取英文單字 → 浮動視窗顯示中文翻譯＋音標 → 一鍵加入單字庫**。

加入的單字會寫進 Google Drive 本機同步資料夾 `VocabularyLog/vocab_YYYY-MM-DD.md`，
之後在 [VocabCloud 網頁](https://addis-vocab.streamlit.app/) 按「☁️ Drive 同步」即可開始練習。

## 架構：常駐 daemon（速度關鍵）

### 選單列開關
daemon 在選單列有一個圖示（雲裡鏤空 A）：
- **停用查單字 / 啟用查單字**：停用時四指點與快捷鍵都不反應；狀態存在 `tools/data/enabled`，重開機後維持
- 開啟今天的單字檔、查看紀錄（/tmp/vocabcloud-daemon.log）
- 重新啟動（launchd 會立刻拉起）
- 結束（卸載到下次登入；重開機/重新登入會自動回來）
圖示由 `tools/make_icons.py` 產生（template 圖，隨深淺色主題自動反轉）。

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
4. 視窗內快速鍵：**空白鍵** 重播發音、**G** 用瀏覽器開 Google 翻譯（翻譯怪怪的時候）、
   **單按 fn**（或 A、⏎）加入單字庫並自動關窗（按鍵看硬體鍵碼，注音輸入法下也能用）、
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

觸發方式：**四指輕點**，由 daemon 自己偵測（`tools/mt_tap.py`，讀 macOS 私有框架
MultitouchSupport 的原始觸控資料，即 BTT 的底層方法，不需權限）。偵測到後先用輔助使用 API
直接讀前景 App 的選取文字（不碰剪貼簿）；該 App 不支援時才退回送 ⌘C 讀剪貼簿。
兩者都需要給 **Python 輔助使用權限**（首次啟動會跳系統提示；若錯過：
系統設定 → 隱私權與安全性 → 輔助使用 → 加入 Python）。不再依賴 BTT。

診斷：`python3 tools/mt_tap.py` 後在觸控板四指輕點，會印出偵測結果。
睡眠/螢幕喚醒後觸控板裝置會被系統重置、回呼失效，daemon 監聽喚醒通知自動重新註冊；
選單列切換啟用時也會重啟偵測器。若四指點突然沒反應，選單列「重新啟動」可強制重來。

備援觸發：daemon 另註冊全域快捷鍵 ⌃⌥⇧⌘F19（任何工具送這組鍵都會查剪貼簿開窗）。
終端機備援：`python3 tools/quick_vocab.py <word>`

## 終端機用法

```bash
python3 tools/quick_vocab.py resilient          # 跳浮動視窗
python3 tools/quick_vocab.py --add resilient    # 直接加入單字庫
python3 tools/quick_vocab.py --lookup resilient # 只印查詢結果 (JSON)
```

## 安裝（換新電腦時）

```bash
cp -R "tools/VocabCloud 查單字.workflow" ~/Library/Services/
/System/Library/CoreServices/pbs -update


```

注意 `.workflow` 裡的 script 路徑與 `quick_vocab.py` 裡的 `VOCAB_LOG_DIR` 寫死了本機路徑，換電腦要改。

## 資料來源（全部線上、免 API Key；MacBook Air 空間有限，不放離線字典）

- **翻譯 / 詞性 / 英文釋義 / 其他譯法：Google 翻譯免金鑰端點**（單字與整句都走這裡，長文品質好；
  實測單字 ~0.1s、整句 ~0.2s，開窗後即顯示）。Google 失敗改用 MyMemory
- 音標（IPA）：[Datamuse](https://www.datamuse.com/api/)，背景並行補上
- 例句＋真人發音 mp3：[dictionaryapi.dev](https://dictionaryapi.dev/)（服務不穩，抓不到就略過）
- 發音：常駐語音子程序（Samantha），開窗即唸；按「🔊」重播，有真人 mp3 就播 mp3
