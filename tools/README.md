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

## 設定鍵盤快捷鍵（建議）

系統設定 → 鍵盤 → 鍵盤快速鍵… → 服務 → 文字 → 找到「VocabCloud 查單字」→ 設定快捷鍵（例如 ⌃⌥D）。

## 終端機用法

```bash
python3 tools/quick_vocab.py resilient          # 跳浮動視窗
python3 tools/quick_vocab.py --add resilient    # 直接加入單字庫
python3 tools/quick_vocab.py --lookup resilient # 只印查詢結果 (JSON)
```

## 安裝快速動作（換新電腦時）

```bash
cp -R "tools/VocabCloud 查單字.workflow" ~/Library/Services/
/System/Library/CoreServices/pbs -update
```

注意 `.workflow` 裡的 script 路徑與 `quick_vocab.py` 裡的 `VOCAB_LOG_DIR` 寫死了本機路徑，換電腦要改。

## 資料來源（全部免 API Key）

- 音標（IPA）/ 詞性 / 英文釋義：[Datamuse](https://www.datamuse.com/api/)
- 例句：[dictionaryapi.dev](https://dictionaryapi.dev/)（服務不穩，抓不到就留空）
- 中文翻譯：[MyMemory](https://mymemory.translated.net/doc/spec.php)
