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

import concurrent.futures
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

_zh_convert = None


def to_traditional(s):
    """簡轉繁。zhconv 載入要數百 ms，延遲到第一次用到才 import。"""
    global _zh_convert
    if _zh_convert is None:
        try:
            from zhconv import convert
            _zh_convert = convert
        except ImportError:
            _zh_convert = False
    return _zh_convert(s, "zh-tw") if _zh_convert else s

ECDICT_DB = Path(__file__).resolve().parent / "data" / "ecdict.db"

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


ECDICT_POS = {
    "n": "n. (名詞)",
    "v": "v. (動詞)",
    "vt": "vt. (及物動詞)",
    "vi": "vi. (不及物動詞)",
    "a": "adj. (形容詞)",
    "ad": "adv. (副詞)",
    "adv": "adv. (副詞)",
    "prep": "prep. (介系詞)",
    "conj": "conj. (連接詞)",
    "pron": "pron. (代名詞)",
    "int": "int. (感嘆詞)",
}


def local_lookup(word):
    """本機 ECDICT 離線字典，毫秒級。查到回傳部分 info dict，查不到回傳 None。"""
    if not ECDICT_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{ECDICT_DB}?mode=ro", uri=True)
        cur = con.cursor()

        def q(w):
            return cur.execute(
                "SELECT word, phonetic, translation, definition FROM stardict "
                "WHERE word = ? COLLATE NOCASE", (w,)
            ).fetchone()

        row = q(word)
        if not row:  # 試著還原常見字尾變化
            lw = word.lower()
            for suf, rep in (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""),
                             ("ing", "e"), ("ed", ""), ("ed", "e")):
                if lw.endswith(suf) and len(lw) - len(suf) >= 3:
                    row = q(lw[: len(lw) - len(suf)] + rep)
                    if row:
                        break
        con.close()
    except Exception:
        return None
    if not row:
        return None

    w, phonetic, translation, definition = row
    info = {"word": w, "phonetic": "", "pos": "", "definition": "", "translation": ""}
    if phonetic:
        info["phonetic"] = f"[{phonetic}]"

    trans_lines = [l.strip() for l in (translation or "").splitlines() if l.strip()]
    if trans_lines:
        m = re.match(r"([a-z]+)\.\s*(.*)", trans_lines[0])
        if m:
            info["pos"] = ECDICT_POS.get(m.group(1), m.group(1) + ".")
        info["translation"] = to_traditional(
            "；".join(re.sub(r"^[a-z]+\.\s*", "", l) for l in trans_lines[:3])
        )

    def_lines = [l.strip() for l in (definition or "").splitlines() if l.strip()]
    if def_lines:
        en_def = re.sub(r"^[a-z]+\.\s*", "", def_lines[0])
        info["definition"] = f"{en_def} / {info['translation']}" if info["translation"] else en_def
    return info


def fetch_datamuse(q):
    """主要來源：Datamuse（音標/詞性/釋義），穩定。"""
    out = {"phonetic": "", "pos": "", "definition": ""}
    results = http_json(
        f"https://api.datamuse.com/words?sp={q}&qe=sp&md=rd&max=1&ipa=1", timeout=6
    )
    if results:
        r = results[0]
        for tag in r.get("tags", []):
            if tag.startswith("ipa_pron:"):
                out["phonetic"] = f"/{tag.split(':', 1)[1].strip()}/"
                break
        defs = r.get("defs", [])
        if defs:
            pos, _, d = defs[0].partition("\t")
            out["pos"] = POS_ZH.get(pos.strip(), pos.strip())
            out["definition"] = d.strip()
    return out


def fetch_dictapi(q):
    """輔助來源：dictionaryapi.dev（例句/音標/真人發音 mp3），常掛，失敗就略過。"""
    out = {"phonetic": "", "example": "", "audio_url": ""}
    entries = http_json(
        f"https://api.dictionaryapi.dev/api/v2/entries/en/{q}", timeout=3
    )
    entry = entries[0]
    for ph in entry.get("phonetics", []):
        if not out["phonetic"] and ph.get("text"):
            out["phonetic"] = ph["text"]
        if not out["audio_url"] and ph.get("audio"):
            out["audio_url"] = ph["audio"]
    for m in entry.get("meanings", []):
        for d in m.get("definitions", []):
            if d.get("example"):
                out["example"] = d["example"]
                break
        if out["example"]:
            break
    return out


def lookup(word):
    """回傳 dict: word, phonetic, pos, definition, example, translation, example_zh, audio_url

    所有網路查詢都並行跑，整體時間 ≈ 最慢的一條，而不是全部加總。
    """
    info = {
        "word": word,
        "phonetic": "",
        "pos": "",
        "definition": "",
        "example": "",
        "translation": "",
        "example_zh": "",
        "audio_url": "",
    }
    q = urllib.parse.quote(word)

    # 本機字典命中 → 只上網補例句（短 timeout，失敗就算了）
    local = local_lookup(word)
    if local:
        info.update(local)
        try:
            da = fetch_dictapi(urllib.parse.quote(info["word"]))
            if da["example"]:
                info["example"] = da["example"]
                info["example_zh"] = translate_zh(da["example"])
            info["audio_url"] = da["audio_url"]
        except Exception:
            pass
        return info

    # 本機查不到（如片語）→ 原網路流程。第一波：三個來源同時查
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f_dm = ex.submit(fetch_datamuse, q)
        f_da = ex.submit(fetch_dictapi, q)
        f_tr = ex.submit(translate_zh, word)

        try:
            info.update(f_dm.result())
        except Exception:
            pass  # 查不到字典仍可翻譯（例如片語）
        try:
            da = f_da.result()
            if da["phonetic"]:
                info["phonetic"] = da["phonetic"]
            info["example"] = da["example"]
            info["audio_url"] = da["audio_url"]
        except Exception:
            pass
        info["translation"] = f_tr.result()

    # 第二波：釋義與例句的中文翻譯同時查
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_def = ex.submit(translate_zh, info["definition"]) if info["definition"] else None
        f_ex = ex.submit(translate_zh, info["example"]) if info["example"] else None
        if f_def:
            zh_def = f_def.result()
            if zh_def:
                info["definition"] = f"{info['definition']} / {zh_def}"
        if f_ex:
            info["example_zh"] = f_ex.result()
    return info


def play_pronunciation(word, audio_url=""):
    """在背景播放發音：優先用字典的真人 mp3，沒有就用系統 TTS。
    完全不阻塞——連英文語音偵測都在子程序做。"""
    script = f'''
import subprocess, re, urllib.request, tempfile, os
audio_url = {audio_url!r}
word = {word!r}
played = False
if audio_url:
    try:
        req = urllib.request.Request(audio_url, headers={{"User-Agent": "VocabCloud/1.0"}})
        data = urllib.request.urlopen(req, timeout=4).read()
        f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        f.write(data); f.close()
        subprocess.run(["afplay", f.name], timeout=10)
        os.unlink(f.name)
        played = True
    except Exception:
        pass
if not played:
    voice = None
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=5).stdout
        for v in ("Samantha", "Alex", "Daniel", "Karen", "Moira", "Fred"):
            if re.search(rf"^{{v}}\\b", out, re.MULTILINE):
                voice = v
                break
    except Exception:
        pass
    cmd = ["say"] + (["-v", voice] if voice else []) + [word]
    subprocess.run(cmd, timeout=10)
'''
    subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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


def run_gui(word):
    """立刻開一個浮動視窗（先顯示查詢中），查詢在背景跑完後原地更新。"""
    import threading
    import tkinter as tk

    root = tk.Tk()
    root.title("VocabCloud")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.configure(padx=22, pady=16)

    # 開在滑鼠游標旁邊
    try:
        px, py = root.winfo_pointerxy()
        root.geometry(f"+{max(px - 160, 20)}+{py + 18}")
    except Exception:
        pass

    WRAP = 360
    word_var = tk.StringVar(value=word)
    phon_var = tk.StringVar(value="")
    trans_var = tk.StringVar(value="查詢中…")
    def_var = tk.StringVar(value="")
    ex_var = tk.StringVar(value="")
    status_var = tk.StringVar(value="")

    tk.Label(root, textvariable=word_var, font=("Helvetica", 30, "bold")).pack(anchor="w")
    tk.Label(root, textvariable=phon_var, font=("Menlo", 15), fg="#6b7280").pack(anchor="w")
    tk.Label(root, textvariable=trans_var, font=("Helvetica", 19, "bold"),
             fg="#047857", wraplength=WRAP, justify="left").pack(anchor="w", pady=(8, 0))
    tk.Label(root, textvariable=def_var, font=("Helvetica", 14),
             wraplength=WRAP, justify="left").pack(anchor="w", pady=(6, 0))
    tk.Label(root, textvariable=ex_var, font=("Helvetica", 13), fg="#2563eb",
             wraplength=WRAP, justify="left").pack(anchor="w", pady=(6, 0))
    tk.Label(root, textvariable=status_var, font=("Helvetica", 12), fg="#b45309").pack(anchor="w", pady=(4, 0))

    btns = tk.Frame(root)
    btns.pack(anchor="e", pady=(12, 0))
    state = {"info": None}

    def on_play():
        info = state["info"]
        play_pronunciation(word, info["audio_url"] if info else "")

    def on_add():
        info = state["info"]
        if not info:
            return
        ok, msg = add_to_log(info)
        status_var.set(msg)
        if ok:
            add_btn.config(state="disabled")
            root.after(1500, root.destroy)

    play_btn = tk.Button(btns, text="🔊 發音", command=on_play)
    add_btn = tk.Button(btns, text="➕ 加入單字庫", command=on_add, state="disabled", default="active")
    close_btn = tk.Button(btns, text="關閉", command=root.destroy)
    play_btn.pack(side="left", padx=4)
    close_btn.pack(side="left", padx=4)
    add_btn.pack(side="left", padx=4)

    root.bind("<Escape>", lambda e: root.destroy())
    root.bind("<Return>", lambda e: on_add())

    def fill(info):
        state["info"] = info
        if info["phonetic"]:
            phon_var.set(info["phonetic"])
        trans_var.set(info["translation"] or "（查無翻譯）")
        parts = []
        if info["pos"]:
            parts.append(f"[{info['pos']}]")
        if info.get("definition"):
            parts.append(info["definition"])
        def_var.set(" ".join(parts))
        if info.get("example"):
            ex = info["example"]
            if info.get("example_zh"):
                ex += f"\n{info['example_zh']}"
            ex_var.set(ex)
        add_btn.config(state="normal")

    def make_overlay():
        """讓視窗浮在所有 Space 與全螢幕 App（如全螢幕 Podcast）之上。"""
        try:
            from AppKit import NSApplication
            root.update_idletasks()
            for win in NSApplication.sharedApplication().windows():
                win.setLevel_(25)  # NSStatusWindowLevel：蓋過全螢幕
                # canJoinAllSpaces (1<<0) + fullScreenAuxiliary (1<<8)
                win.setCollectionBehavior_(win.collectionBehavior() | (1 << 0) | (1 << 8))
        except Exception:
            pass

    def start():
        """視窗畫出來之後才做的事：查字典、發音、補例句。"""
        make_overlay()
        play_pronunciation(word)  # 立刻用系統 TTS 唸一次，不等網路

        local = local_lookup(word)  # 毫秒級
        if local:
            fill({**{"example": "", "example_zh": "", "audio_url": ""}, **local})

            def worker():  # 背景只補例句與真人發音
                info = dict(state["info"])
                try:
                    da = fetch_dictapi(urllib.parse.quote(info["word"]))
                    if da["example"]:
                        info["example"] = da["example"]
                        info["example_zh"] = translate_zh(da["example"])
                    info["audio_url"] = da["audio_url"]
                    root.after(0, fill, info)
                except Exception:
                    pass
        else:
            def worker():  # 本機查不到（片語等）→ 走網路
                info = lookup(word)
                try:
                    root.after(0, fill, info)
                except Exception:
                    pass  # 視窗已被關掉

        threading.Thread(target=worker, daemon=True).start()

    root.after(90_000, root.destroy)  # 90 秒沒動作自動關閉
    root.lift()
    root.focus_force()
    root.after(1, start)  # 先讓視窗立刻畫出來，其餘延後
    root.mainloop()


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

    if mode == "dialog":
        run_gui(word)
        return

    info = lookup(word)

    if mode == "lookup":
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return

    if mode == "add":
        ok, msg = add_to_log(info)
        notify("VocabCloud", msg)
        print(msg)


if __name__ == "__main__":
    main()
