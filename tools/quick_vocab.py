#!/usr/bin/env python3
"""快速查單字 + 一鍵加入 VocabCloud 單字庫。

用法:
    python3 quick_vocab.py <word>            # 查字並跳出浮動視窗，可一鍵加入字庫
    python3 quick_vocab.py --add <word>      # 直接加入字庫（不跳視窗）
    python3 quick_vocab.py --lookup <word>   # 只印出查詢結果（測試用）

資料來源（免 API Key）:
    - 音標/詞性/釋義: Datamuse（穩定）
    - 例句: dictionaryapi.dev（不穩定，查不到就略過）
    - 中文翻譯: MyMemory

加入字庫 = 把單字區塊寫進 Google Drive 本機同步資料夾的
VocabularyLog/vocab_YYYY-MM-DD.md，之後在 VocabCloud 網頁按「☁️ Drive 同步」匯入。
"""

import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

VOCAB_LOG_DIR = Path.home() / "Library/CloudStorage/GoogleDrive-addislyu@gmail.com/我的雲端硬碟/VocabularyLog"

POS_ZH = {
    "n": "n. (名詞)",
    "v": "v. (動詞)",
    "adj": "adj. (形容詞)",
    "adv": "adv. (副詞)",
    "prep": "prep. (介系詞)",
    "conj": "conj. (連接詞)",
    "pron": "pron. (代名詞)",
    "int": "int. (感嘆詞)",
}


def http_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "VocabCloud/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def translate_zh(text):
    """用 MyMemory 把英文翻成繁體中文，失敗回傳空字串。"""
    if not text:
        return ""
    try:
        q = urllib.parse.quote(text)
        data = http_json(
            f"https://api.mymemory.translated.net/get?q={q}&langpair=en|zh-TW"
        )
        zh = data.get("responseData", {}).get("translatedText", "") or ""
        # MyMemory 偶爾回傳原文或錯誤訊息
        if zh.upper().startswith("MYMEMORY WARNING") or zh.strip().lower() == text.strip().lower():
            return ""
        return zh.strip()
    except Exception:
        return ""


def lookup(word):
    """回傳 dict: word, phonetic, pos, definition, example, translation, example_zh"""
    info = {
        "word": word,
        "phonetic": "",
        "pos": "",
        "definition": "",
        "example": "",
        "translation": "",
        "example_zh": "",
    }

    q = urllib.parse.quote(word)

    # 主要來源：Datamuse（音標/詞性/釋義）
    try:
        results = http_json(
            f"https://api.datamuse.com/words?sp={q}&qe=sp&md=rd&max=1&ipa=1"
        )
        if results:
            r = results[0]
            for tag in r.get("tags", []):
                if tag.startswith("ipa_pron:"):
                    info["phonetic"] = f"/{tag.split(':', 1)[1].strip()}/"
                    break
            defs = r.get("defs", [])
            if defs:
                pos, _, d = defs[0].partition("\t")
                info["pos"] = POS_ZH.get(pos.strip(), pos.strip())
                info["definition"] = d.strip()
    except Exception:
        pass  # 查不到字典仍可翻譯（例如片語）

    # 輔助來源：dictionaryapi.dev（例句、較標準的音標），常掛，失敗就略過
    try:
        entries = http_json(
            f"https://api.dictionaryapi.dev/api/v2/entries/en/{q}", timeout=4
        )
        entry = entries[0]
        for ph in entry.get("phonetics", []):
            if ph.get("text"):
                info["phonetic"] = ph["text"]
                break
        for m in entry.get("meanings", []):
            for d in m.get("definitions", []):
                if d.get("example"):
                    info["example"] = d["example"]
                    break
            if info["example"]:
                break
    except Exception:
        pass

    info["translation"] = translate_zh(word)
    if info["definition"]:
        zh_def = translate_zh(info["definition"])
        if zh_def:
            info["definition"] = f"{info['definition']} / {zh_def}"
    if info["example"]:
        info["example_zh"] = translate_zh(info["example"])
    return info


def already_in_log(word):
    """掃描 VocabularyLog 所有 md，看單字是否已存在。"""
    pat = re.compile(rf"^###\s+{re.escape(word)}\s*$", re.IGNORECASE | re.MULTILINE)
    try:
        for f in VOCAB_LOG_DIR.glob("*.md"):
            if pat.search(f.read_text(encoding="utf-8", errors="ignore")):
                return True
    except OSError:
        pass
    return False


def add_to_log(info):
    """把單字區塊附加到今天的 vocab_YYYY-MM-DD.md。回傳 (ok, message)。"""
    if not VOCAB_LOG_DIR.exists():
        return False, f"找不到 VocabularyLog 資料夾：{VOCAB_LOG_DIR}"
    word = info["word"]
    if already_in_log(word):
        return False, f"「{word}」已經在單字庫裡了"

    block = (
        f"### {word}\n"
        f"* **KK音標：** {info['phonetic']}\n"
        f"* **詞性：** {info['pos']}\n"
        f"* **釋義：** {info['definition']}\n"
        f"* **例句：** {info['example']}\n"
        f"* **翻譯：** {info['example_zh'] or info['translation']}\n"
        f"\n---\n\n"
    )
    log_file = VOCAB_LOG_DIR / f"vocab_{date.today().isoformat()}.md"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(block)
    return True, f"「{word}」已加入單字庫（{log_file.name}）"


def notify(title, message):
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{esc(message)}" with title "{esc(title)}"'],
        check=False,
    )


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def show_dialog(info):
    """浮動視窗顯示查詢結果；按「加入單字庫」回傳 True。"""
    lines = [info["word"]]
    if info["phonetic"]:
        lines[0] += f"  {info['phonetic']}"
    if info["translation"]:
        lines.append(f"🀄 {info['translation']}")
    if info["pos"]:
        lines.append(f"📎 {info['pos']}")
    if info["definition"]:
        lines.append(f"📖 {info['definition']}")
    if info["example"]:
        ex = info["example"]
        if info["example_zh"]:
            ex += f"\n　  {info['example_zh']}"
        lines.append(f"✏️ {ex}")
    if len(lines) == 1:
        lines.append("（查無資料，仍可加入單字庫稍後補充）")
    text = "\n\n".join(lines)

    script = (
        f'display dialog "{esc(text)}" with title "VocabCloud 查單字" '
        f'buttons {{"關閉", "➕ 加入單字庫"}} default button "➕ 加入單字庫" '
        f'with icon note giving up after 60'
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, check=False
    )
    return "加入單字庫" in (result.stdout or "")


def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    mode = "dialog"
    if args and args[0] in ("--add", "--lookup"):
        mode = args.pop(0)[2:]
    if not args:
        print(__doc__)
        sys.exit(1)

    word = " ".join(args).strip().strip('.,;:!?"\'()[]').strip()
    if not word:
        sys.exit(1)

    info = lookup(word)

    if mode == "lookup":
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return

    if mode == "add":
        ok, msg = add_to_log(info)
        notify("VocabCloud", msg)
        print(msg)
        return

    # dialog 模式
    if show_dialog(info):
        ok, msg = add_to_log(info)
        notify("VocabCloud", msg)


if __name__ == "__main__":
    main()
