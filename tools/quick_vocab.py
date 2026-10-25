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
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

_zh_convert = None


def to_traditional(s):
    """簡轉繁（ECDICT 的中譯是簡體；沒有同等規模的原生繁體離線英漢字典）。
    優先 OpenCC s2twp：台灣正體＋台灣用語（信息→資訊、軟件→軟體）；
    沒裝就退回 zhconv。載入要數百 ms，延遲到第一次用到才 import（daemon 啟動時預載）。"""
    global _zh_convert
    if _zh_convert is None:
        try:
            from opencc import OpenCC
            _zh_convert = OpenCC("s2twp").convert
        except ImportError:
            try:
                from zhconv import convert
                _zh_convert = lambda s: convert(s, "zh-tw")
            except ImportError:
                _zh_convert = False
    return _zh_convert(s) if _zh_convert else s

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


_ecdict_con = None


def _get_ecdict_con():
    global _ecdict_con
    if _ecdict_con is None:
        _ecdict_con = sqlite3.connect(
            f"file:{ECDICT_DB}?mode=ro", uri=True, check_same_thread=False
        )
    return _ecdict_con


def local_lookup(word):
    """本機 ECDICT 離線字典，毫秒級。查到回傳部分 info dict，查不到回傳 None。"""
    if not ECDICT_DB.exists():
        return None
    try:
        con = _get_ecdict_con()
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
    except Exception:
        return None
    if not row:
        return None
    return _row_to_info(row)


def _phonetic_of(w):
    try:
        row = _get_ecdict_con().execute(
            "SELECT phonetic FROM stardict WHERE word = ? COLLATE NOCASE", (w,)
        ).fetchone()
        return (row[0] or "").strip() if row else ""
    except Exception:
        return ""


def _ed_sound(base):
    if base.endswith(("t", "d")):
        return "id"
    return "t" if base[-1:] in "pkfsθʃ" else "d"


def _s_sound(base):
    if base[-1:] in "szʃʒ" or base.endswith("dʒ"):
        return "iz"
    return "s" if base[-1:] in "pktfθ" else "z"


# 字尾 → 發音（不改變重音的字尾才放進來）
_DERIV_SUFFIXES = (("ness", "nis"), ("less", "lis"), ("ment", "mәnt"), ("ful", "ful"),
                   ("ly", "li"), ("ally", "әli"), ("er", "ә"), ("ing", "iŋ"),
                   ("ed", None), ("es", None), ("s", None))


def derive_phonetic(word):
    """ECDICT 約 6% 常用字沒音標，幾乎都是衍生字/變化形（willingness、amplifying）。
    用原形的音標加上字尾發音推導出來，純本機、毫秒級。推不出來回傳空字串。"""
    w = word.lower()
    if " " in w:  # 片語：每個字的音標接起來（set apart → set + apart）
        parts = [_phonetic_of(p) or derive_phonetic(p) for p in w.split()]
        return " ".join(parts) if all(parts) else ""

    try:  # 1. ECDICT 的 exchange 欄位直接記錄原形與變化類型
        row = _get_ecdict_con().execute(
            "SELECT exchange FROM stardict WHERE word = ? COLLATE NOCASE", (w,)
        ).fetchone()
        ex = dict(p.split(":", 1) for p in (row[0] or "").split("/") if ":" in p) if row else {}
        lemma, kind = ex.get("0"), ex.get("1", "")
        if lemma and kind:
            base = _phonetic_of(lemma)
            if base:
                k = kind[0]
                suffix = {"i": "iŋ", "r": "ә", "t": "ist"}.get(k)
                if k in "pd":
                    suffix = _ed_sound(base)
                elif k in "3s":
                    suffix = _s_sound(base)
                if suffix:
                    return base + suffix
    except Exception:
        pass

    for suf, sound in _DERIV_SUFFIXES:  # 2. 剝字尾找原形
        if not w.endswith(suf) or len(w) - len(suf) < 3:
            continue
        stem = w[: -len(suf)]
        candidates = [stem, stem + "e"]
        if stem.endswith("i"):
            candidates.append(stem[:-1] + "y")          # happi→happy
        if len(stem) > 3 and stem[-1] == stem[-2]:
            candidates.append(stem[:-1])                # admitt→admit
        for c in candidates:
            base = _phonetic_of(c)
            if base:
                if sound is None:
                    sound = _ed_sound(base) if suf == "ed" else _s_sound(base)
                return base + sound
    return ""


def _row_to_info(row):
    """把 ECDICT 的一列轉成 info dict（共用於單字查詢與片語偵測）。"""
    w, phonetic, translation, definition = row
    info = {"word": w, "phonetic": "", "pos": "", "definition": "", "translation": ""}
    phonetic = (phonetic or "").strip() or derive_phonetic(w)
    if phonetic:
        info["phonetic"] = f"[{phonetic}]"

    trans_lines = [l.strip() for l in (translation or "").splitlines() if l.strip()]
    if trans_lines:
        m = re.match(r"([a-z]+)\.\s*(.*)", trans_lines[0])
        if m:
            info["pos"] = ECDICT_POS.get(m.group(1), m.group(1) + ".")
        info["translation"] = to_traditional(
            "；".join(re.sub(r"^[a-z]+\.\s*", "", l) for l in trans_lines[:2])
        )

    def_lines = [l.strip() for l in (definition or "").splitlines() if l.strip()]
    if def_lines:
        en_def = re.sub(r"^[a-z]+\.\s*", "", def_lines[0])
        info["definition"] = f"{en_def} / {info['translation']}" if info["translation"] else en_def
    return info


_STOPWORDS = {"a", "an", "the", "is", "are", "was", "were", "be", "been", "it",
              "its", "this", "that", "what", "who", "and", "or", "but", "so",
              "of", "to", "in", "on", "at", "as", "by", "he", "she", "they", "we"}


def _word_forms(w):
    """還原常見字尾變化：sets→set、setting→set…（含原形）"""
    forms = [w]
    for suf, rep in (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""),
                     ("ing", "e"), ("ed", ""), ("ed", "e")):
        if w.endswith(suf) and len(w) - len(suf) >= 2:
            forms.append(w[: len(w) - len(suf)] + rep)
    return forms


def find_phrases(text, limit=3):
    """在一段選取文字中自動偵測 ECDICT 收錄的片語。

    掃描 2~4 字的連續組合，以及「動詞 … 介係詞」被 1~2 個字隔開的組合
    （sets Xiang apart → set apart），第一個字做字尾還原。
    回傳 info dict list，長的/連續的優先。
    """
    if not ECDICT_DB.exists():
        return []
    words = [w.lower() for w in re.findall(r"[A-Za-z']+", text)]
    n = len(words)
    candidates = []  # (score, phrase)

    for i in range(n):
        for length in (4, 3, 2):  # 連續 n-gram，長的分數高
            if i + length <= n:
                rest = words[i + 1: i + length]
                for f in _word_forms(words[i]):
                    candidates.append((length * 10, " ".join([f] + rest)))
        for gap in (1, 2):  # 隔開的動詞片語：set ... apart
            j = i + gap + 1
            if j < n:
                for f in _word_forms(words[i]):
                    candidates.append((15, f"{f} {words[j]}"))

    seen, results = set(), []
    try:
        cur = _get_ecdict_con().cursor()
        for score, phrase in sorted(candidates, key=lambda c: -c[0]):
            if phrase in seen or " " not in phrase:
                continue
            seen.add(phrase)
            parts = phrase.split()
            if all(p in _STOPWORDS for p in parts):
                continue
            row = cur.execute(
                "SELECT word, phonetic, translation, definition FROM stardict "
                "WHERE word = ? COLLATE NOCASE", (phrase,)
            ).fetchone()
            if row:
                info = _row_to_info(row)
                if info["translation"] or info["definition"]:
                    results.append(info)
                    if len(results) >= limit:
                        break
    except Exception:
        pass
    return results


def is_phrase(text):
    """三個字以上或太長 → 當片語/整句處理（只翻譯，不查音標字典）。"""
    return len(text.split()) >= 3 or len(text) > 24


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
    # 片語/整句：翻譯整句，並自動偵測其中的字典片語（set apart 等）
    if is_phrase(word):
        sentence_zh = translate_zh(word)
        matches = find_phrases(word)
        if matches:
            best = matches[0]
            info.update(best)          # 片語當主條目
            info["example"] = word     # 原選取整句變例句
            info["example_zh"] = sentence_zh
            if len(matches) > 1:
                info["others"] = [m["word"] for m in matches[1:]]
        else:
            info["translation"] = sentence_zh
        return info

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


_english_voice = None


def get_english_voice():
    """找系統英文語音（say 用），結果快取。約需 0.3s，daemon 啟動時先呼叫一次。"""
    global _english_voice
    if _english_voice is None:
        _english_voice = ""
        try:
            out = subprocess.run(
                ["say", "-v", "?"], capture_output=True, text=True, timeout=5
            ).stdout
            for v in ("Samantha", "Alex", "Daniel", "Karen", "Moira", "Fred"):
                if re.search(rf"^{v}\b", out, re.MULTILINE):
                    _english_voice = v
                    break
        except Exception:
            pass
    return _english_voice


def play_pronunciation(word, audio_url=""):
    """在背景播放發音：優先用字典的真人 mp3，沒有就用系統 TTS。
    沒有 mp3 時直接 Popen say（毫秒級）；有 mp3 才開子程序下載播放。"""
    if not audio_url:
        voice = get_english_voice()
        # 備援路徑（CLI/一次性模式）才會走到這：墊靜音避免裝置喚醒截掉開頭。
        # daemon 模式用常駐 NSSpeechSynthesizer＋音訊保溫，無此問題。
        cmd = ["say"] + (["-v", voice] if voice else []) + [f"[[slnc 350]] {word}"]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    script = f'''
import subprocess, urllib.request, tempfile, os
audio_url = {audio_url!r}
word = {word!r}
voice = {get_english_voice()!r}
played = False
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
    cmd = ["say"] + (["-v", voice] if voice else []) + ["[[slnc 350]] " + word]
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


SOCKET_PATH = f"/tmp/vocabcloud-{os.getuid()}.sock"


class VocabWindow:
    """浮動查字視窗。daemon 模式下預先建好、隱藏待命，喚醒是毫秒級。"""

    def __init__(self, oneshot):
        import tkinter as tk

        self.tk = tk
        self.oneshot = oneshot
        self.word = ""
        self.gen = 0          # 世代計數，避免舊查詢結果蓋掉新單字
        self.hide_job = None
        self.focus_job = None
        self.saw_active = False
        self.info = None
        self.pending_add = False

        root = self.root = tk.Tk()
        root.title("VocabCloud")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        root.configure(padx=22, pady=16)

        WRAP = 360
        self.word_var = tk.StringVar()
        self.phon_var = tk.StringVar()
        self.trans_var = tk.StringVar()
        self.def_var = tk.StringVar()
        self.ex_var = tk.StringVar()
        self.status_var = tk.StringVar()

        # 按鈕與狀態列先 pack 在底部：空間不夠時優先保住按鈕，不會被截掉
        btns = tk.Frame(root)
        btns.pack(side="bottom", anchor="e", pady=(12, 0))
        self.play_btn = tk.Button(btns, text="🔊 發音 (空白鍵)", command=self.on_play)
        close_btn = tk.Button(btns, text="關閉 (Esc)", command=self.dismiss)
        self.add_btn = tk.Button(btns, text="➕ 加入單字庫 (fn)", command=self.on_add,
                                 state="disabled", default="active")
        self.play_btn.pack(side="left", padx=4)
        close_btn.pack(side="left", padx=4)
        self.add_btn.pack(side="left", padx=4)
        self.status_label = tk.Label(root, textvariable=self.status_var,
                                     font=("Helvetica", 14, "bold"), fg="#b45309")
        self.status_label.pack(side="bottom", anchor="w", pady=(4, 0))

        self.word_label = tk.Label(root, textvariable=self.word_var,
                                   font=("Helvetica", 30, "bold"),
                                   wraplength=WRAP, justify="left")
        self.word_label.pack(anchor="w")
        tk.Label(root, textvariable=self.phon_var, font=("Menlo", 15), fg="#6b7280",
                 wraplength=WRAP, justify="left").pack(anchor="w")
        tk.Label(root, textvariable=self.trans_var, font=("Helvetica", 19, "bold"),
                 fg="#047857", wraplength=WRAP, justify="left").pack(anchor="w", pady=(8, 0))
        tk.Label(root, textvariable=self.def_var, font=("Helvetica", 14),
                 wraplength=WRAP, justify="left").pack(anchor="w", pady=(6, 0))
        tk.Label(root, textvariable=self.ex_var, font=("Helvetica", 13), fg="#2563eb",
                 wraplength=WRAP, justify="left").pack(anchor="w", pady=(6, 0))

        root.bind("<Escape>", lambda e: self.dismiss())
        root.bind("<Return>", lambda e: self.on_add())
        root.bind("a", lambda e: self.on_add())      # 左手無名指原位
        root.bind("A", lambda e: self.on_add())
        root.bind("<space>", lambda e: self.on_play())
        root.bind("<FocusOut>", self._on_focus_out)  # 點視窗外面 → 自動關閉
        root.protocol("WM_DELETE_WINDOW", self.dismiss)

        self._setup_macos()
        self._setup_speech()
        self._setup_fn_key()

    def _setup_fn_key(self):
        """單按 fn 加入單字庫。Tk 收不到單獨的 fn，改用 NSEvent 本機監聽
        （只在本視窗作用中時觸發，不需權限）。只認「單按放開」：按住 fn 期間
        有按其他鍵（fn+方向鍵、fn+Delete…）就不算，避免組合鍵誤觸。
        監聽回呼發生在 Tk 處理事件途中，回呼裡碰 tkinter 會讓直譯器崩潰，
        所以回呼只記下來，由 Tk 每 10ms 檢查。"""
        self.fn_hits = []
        state = {"down_at": None, "clean": False}
        try:
            from AppKit import NSEvent

            FN_FLAG = 1 << 23            # NSEventModifierFlagFunction
            KEYCODE_FN = 63              # kVK_Function
            MASK = (1 << 12) | (1 << 10)  # FlagsChanged | KeyDown

            def handler(event):
                try:
                    if event.type() == 10:  # KeyDown：fn 按住時按了別的鍵 → 是組合鍵
                        state["clean"] = False
                    elif event.keyCode() == KEYCODE_FN:
                        if event.modifierFlags() & FN_FLAG:
                            state["down_at"], state["clean"] = time.time(), True
                        elif state["down_at"] is not None:
                            if state["clean"] and time.time() - state["down_at"] < 0.6:
                                self.fn_hits.append(1)
                            state["down_at"] = None
                except Exception:
                    pass
                return event

            self._fn_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                MASK, handler)
        except Exception as e:
            _dbg(f"fn monitor failed: {e}")
            return
        self._poll_fn()

    def _poll_fn(self):
        if self.fn_hits:
            self.fn_hits.clear()
            if self.root.state() == "normal":
                self.on_add()
        self.root.after(10, self._poll_fn)

    # ---------- macOS 視窗層級：跨 Space、蓋過全螢幕 App ----------
    def _setup_macos(self):
        self.nsapp = None
        try:
            from AppKit import NSApplication
            self.nsapp = NSApplication.sharedApplication()
            self.nsapp.setActivationPolicy_(1)  # accessory：不佔 Dock、不搶全螢幕
            self.root.update_idletasks()
            for w in self.nsapp.windows():
                w.setLevel_(25)  # NSStatusWindowLevel
                # canJoinAllSpaces (1<<0) + fullScreenAuxiliary (1<<8)
                w.setCollectionBehavior_(w.collectionBehavior() | (1 << 0) | (1 << 8))
                w.setTabbingMode_(2)  # 不允許 macOS 視窗分頁（避免 Console 併成分頁）
        except Exception:
            pass
        try:
            self.root.tk.call("console", "hide")  # 藏掉 Tk 的 Console 視窗
        except Exception:
            pass

    def _setup_speech(self):
        """常駐的行程內語音合成器：發音不再每次冷啟動 say（省 ~100ms 且不截頭）。"""
        self.synth = None
        try:
            from AppKit import NSSpeechSynthesizer
            voice = None
            for v in NSSpeechSynthesizer.availableVoices():
                s = str(v)
                if "en-US" in s or ".en_US." in s:
                    voice = v
                    if "Samantha" in s:
                        break
            if voice:
                self.synth = NSSpeechSynthesizer.alloc().initWithVoice_(voice)
        except Exception:
            self.synth = None

    def _speak(self, text, audio_url=""):
        if audio_url:  # 有真人 mp3 → 交給子程序下載播放
            play_pronunciation(text, audio_url)
            return
        if self.synth is not None:
            try:
                self.synth.stopSpeaking()
                self.synth.startSpeakingString_(text)
                return
            except Exception:
                pass
        play_pronunciation(text)  # 備援：say

    def _order_front(self, activate=True):
        """activate=False：只浮到最上層、不搶焦點（BTT 的 ⌘C 還要送到原 App）。"""
        try:
            if self.nsapp:
                if activate:
                    self.nsapp.unhideWithoutActivation()  # dismiss 時 hide 過
                    self.nsapp.activateIgnoringOtherApps_(True)
                for w in self.nsapp.windows():
                    w.orderFrontRegardless()
                    if activate:
                        w.makeKeyAndOrderFront_(None)
        except Exception:
            pass
        if not activate:
            return
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass


    # ---------- 顯示/隱藏 ----------
    # 注意：不能在 ⌘C 完成前開窗。Tk 的 deiconify 會讓本 App 變成前景，
    # BTT 送的 ⌘C 就會打到這個視窗而不是原 App，導致複製失敗。
    def show(self, word):
        import threading

        word = word.strip().strip(".,;:!?\"'()[]").strip()
        if not word:
            return
        self.gen += 1
        gen = self.gen
        self.word = word
        self.info = None
        self.pending_add = False
        self.word_var.set(word)
        self.phon_var.set("")
        self.trans_var.set("查詢中…")
        self.def_var.set("")
        self.ex_var.set("")
        self.status_var.set("")
        self.add_btn.config(state="disabled", text="➕ 加入單字庫 (fn)")
        # 片語/整句：縮小標題字體
        self.word_label.config(
            font=("Helvetica", 16 if is_phrase(word) else 30, "bold")
        )

        try:
            self.pointer = self.root.winfo_pointerxy()
        except Exception:
            self.pointer = (200, 200)
        self._place()
        self.root.deiconify()
        self._order_front()
        self._reset_hide_timer()
        self._watch_focus()

        self._speak(word)  # 立刻發音（常駐合成器，零冷啟動）

        empty = {"word": word, "phonetic": "", "pos": "", "definition": "",
                 "example": "", "translation": "", "example_zh": "", "audio_url": ""}

        def worker():
            if is_phrase(word):
                # 片語偵測是純本機（毫秒級）：命中就先開窗顯示片語條目，
                # 整句翻譯（網路，較慢）在背景補上 → 跟單字一樣快
                matches = find_phrases(word)
                if matches:
                    info = {**empty, **matches[0], "example": word}
                    if len(matches) > 1:
                        info["others"] = [m["word"] for m in matches[1:]]
                    self._post_fill(info, gen)
                    info = {**info, "example_zh": translate_zh(word)}
                    self._post_fill(info, gen)
                else:  # 沒有字典片語 → 整句翻譯
                    self._post_fill({**empty, "translation": translate_zh(word)}, gen)
                return

            local = local_lookup(word)  # 毫秒級
            if local:
                info = {**empty, **local}
                self._post_fill(info, gen)
                if not info["phonetic"]:  # 本機推導不出來 → 背景向 Datamuse 要音標
                    try:
                        ph = fetch_datamuse(urllib.parse.quote(info["word"]))["phonetic"]
                        if ph:
                            info = {**info, "phonetic": ph}
                            self._post_fill(info, gen)
                    except Exception:
                        pass
                try:  # 背景補例句與真人發音
                    da = fetch_dictapi(urllib.parse.quote(info["word"]))
                    info = dict(info)
                    if da["example"]:
                        info["example"] = da["example"]
                        info["example_zh"] = translate_zh(da["example"])
                    info["audio_url"] = da["audio_url"]
                    self._post_fill(info, gen)
                except Exception:
                    pass
            else:  # 本機查不到 → 走網路
                self._post_fill(lookup(word), gen)

        threading.Thread(target=worker, daemon=True).start()

    def _post_fill(self, info, gen):
        try:
            self.root.after(0, lambda: self.fill(info, gen))
        except Exception:
            pass  # 視窗已關閉

    def fill(self, info, gen):
        if gen != self.gen:
            return  # 已經在查別的字了
        self.info = info
        if info["word"] != self.word:  # 片語偵測把主條目換成了字典片語
            self.word_var.set(info["word"])
            self.word_label.config(
                font=("Helvetica", 16 if is_phrase(info["word"]) else 30, "bold")
            )
        if info["phonetic"]:
            self.phon_var.set(info["phonetic"])
        self.trans_var.set(info["translation"] or "（查無翻譯）")
        parts = []
        if info["pos"]:
            parts.append(f"[{info['pos']}]")
        if info.get("definition"):
            # 視窗只顯示英文釋義；中文翻譯上面綠字已有，不重複（存進字庫仍是完整版）
            parts.append(info["definition"].split(" / ")[0])
        if info.get("others"):
            parts.append("🧩 其他片語: " + "、".join(info["others"]))
        self.def_var.set(" ".join(parts))
        if info.get("example"):
            ex = info["example"]
            if info.get("example_zh"):
                ex += f"\n{info['example_zh']}"
            self.ex_var.set(ex)
        self.add_btn.config(state="normal")
        self._place()  # 內容變多視窗會長高，重新調整避免超出螢幕
        if self.pending_add:  # 資料還沒好時使用者就按了 ⌘ → 現在自動完成加入
            self.pending_add = False
            self.on_add()

    def _place(self):
        """開在選字的另一側（游標在左半邊→貼右邊，反之貼左邊），和那一行切齊，
        不蓋住選的字和正在讀的段落；兩側都放不下才退回游標上/下方。"""
        try:
            self.root.update_idletasks()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            w = min(self.root.winfo_reqwidth(), sw - 40)
            h = min(self.root.winfo_reqheight(), sh - 80)
            px, py = self.pointer
            gap = 60
            right_x, left_x = sw - w - 20, 20
            fits_right = right_x >= px + gap
            fits_left = left_x + w <= px - gap
            if fits_right and (px < sw / 2 or not fits_left):
                x, side = right_x, True
            elif fits_left:
                x, side = left_x, True
            else:
                x, side = min(max(px - w // 2, 20), max(sw - w - 20, 20)), False
            if side:
                y = min(max(py - 40, 20), max(sh - h - 60, 20))  # 頂端和選字那行切齊
            else:
                y = py + 18                      # 兩側都放不下 → 游標下方
                if y + h > sh - 60:              # 下面放不下 → 游標上方
                    y = py - h - 24
                if y < 20:
                    y = max(20, sh - h - 60)
            # 明確指定尺寸：macOS 上只設位置的話，視窗高度不會跟著內容長大
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _on_focus_out(self, _event=None):
        def check():
            try:
                if self.root.focus_displayof() is None and self.root.state() == "normal":
                    self.dismiss()
            except Exception:
                pass
        try:
            self.root.after(150, check)
        except Exception:
            pass

    def _watch_focus(self):
        """點視窗外面自動關閉：輪詢 NSApp 作用中狀態。
        （FocusOut 事件在 accessory App 的 Tk 視窗上不可靠，故用輪詢）"""
        if self.focus_job:
            try:
                self.root.after_cancel(self.focus_job)
            except Exception:
                pass
            self.focus_job = None
        self.saw_active = False

        def loop():
            self.focus_job = None
            if self.root.state() != "normal":
                return  # 已收起
            active = True
            try:
                if self.nsapp is not None:
                    active = bool(self.nsapp.isActive())
            except Exception:
                pass
            if active:
                self.saw_active = True
            elif self.saw_active:  # 曾取得焦點、現在失去 → 使用者點了外面
                self.dismiss()
                return
            self.focus_job = self.root.after(200, loop)

        self.focus_job = self.root.after(400, loop)  # 給視窗一點時間完成 activate

    def _reset_hide_timer(self):
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
        self.hide_job = self.root.after(90_000, self.dismiss)  # 90 秒自動收起

    def dismiss(self):
        if self.oneshot:
            self.root.destroy()
            return
        self.root.withdraw()
        try:  # 交還焦點給原 App（Podcast），不然下一次 ⌘C 可能打到我們這裡
            if self.nsapp is not None and self.nsapp.isActive():
                self.nsapp.hide_(None)
        except Exception:
            pass

    # ---------- 按鈕 ----------
    def on_play(self):
        self._speak(self.word, self.info.get("audio_url", "") if self.info else "")

    def on_add(self):
        if not self.info:  # 查詢還沒完成：立刻關窗，fill() 完成後在背景自動加入
            self.pending_add = True
            if self.oneshot:  # 一次性模式關窗=程式結束，只能留窗等
                self.status_label.config(fg="#6b7280")
                self.status_var.set("⏳ 查詢中，完成後自動加入…")
            else:
                self.dismiss()  # daemon 模式：視窗先收，結果看系統通知
            return
        info = self.info
        if self.oneshot:  # 一次性模式關窗=程式結束，只能先寫完再關
            ok, msg = add_to_log(info)
            notify("VocabCloud", msg)
            self.dismiss()
            return
        # 先關窗再寫檔：查重複(~80ms)＋系統通知(~170ms)都丟背景，按下去視窗立刻消失
        self.dismiss()

        def work():
            ok, msg = add_to_log(info)
            notify("VocabCloud", msg)

        import threading
        threading.Thread(target=work, daemon=True).start()


def send_to_daemon(word):
    """把單字丟給常駐 daemon（毫秒級）。daemon 沒在跑回傳 False。"""
    import socket

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(SOCKET_PATH)
        s.sendall(word.encode("utf-8"))
        s.close()
        return True
    except Exception:
        return False


def _start_audio_keepalive():
    """背景無限循環播放靜音（混音，不影響其他聲音）。
    像 podcast 播放器一樣一直握著音訊通道，發音才不會被裝置喚醒截掉開頭。
    迴圈以 kill -0 檢查 daemon 是否還活著，daemon 結束迴圈就自行退出。"""
    try:
        wav = ECDICT_DB.parent / "silence.wav"
        if not wav.exists():
            import struct
            import wave
            wav.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(wav), "w") as f:
                f.setnchannels(1)
                f.setsampwidth(2)
                f.setframerate(8000)
                f.writeframes(struct.pack("<h", 0) * 8000 * 60)  # 60 秒靜音
        subprocess.Popen(
            ["/bin/sh", "-c",
             f'while kill -0 {os.getpid()} 2>/dev/null; do afplay "{wav}"; done'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _dbg(msg):
    """計時紀錄寫到 daemon 的 stderr（launchd 導向 /tmp/vocabcloud-daemon.log）。"""
    print(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {msg}",
          file=sys.stderr, flush=True)


HOTKEY_KEYCODE = 80                      # F19
HOTKEY_MODS = 256 | 512 | 2048 | 4096    # ⌘ ⇧ ⌥ ⌃（Carbon 修飾鍵旗標）
_hotkey_refs = []                        # 保住 ctypes callback，避免被 GC


def _register_hotkey(callback):
    """用 Carbon RegisterEventHotKey 註冊全域快捷鍵 ⌃⌥⇧⌘F19（不需輔助使用權限）。
    BTT 送這組鍵當作「查字」訊號：比 BTT 執行 shell 指令快約 0.5 秒。"""
    import ctypes

    try:
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    except OSError:
        return False

    def fourcc(s):
        return int.from_bytes(s.encode(), "big")

    class EventTypeSpec(ctypes.Structure):
        _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

    class EventHotKeyID(ctypes.Structure):
        _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

    Handler = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

    def on_hotkey(_call_ref, _event, _data):
        try:
            callback()
        except Exception:
            pass
        return 0

    handler = Handler(on_hotkey)
    carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
    carbon.InstallEventHandler.argtypes = [
        ctypes.c_void_p, Handler, ctypes.c_uint32, ctypes.POINTER(EventTypeSpec),
        ctypes.c_void_p, ctypes.c_void_p]
    carbon.RegisterEventHotKey.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, EventHotKeyID, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]

    target = carbon.GetApplicationEventTarget()
    spec = EventTypeSpec(fourcc("keyb"), 5)  # kEventClassKeyboard / kEventHotKeyPressed
    if carbon.InstallEventHandler(target, handler, 1, ctypes.byref(spec), None, None) != 0:
        return False
    ref = ctypes.c_void_p()
    status = carbon.RegisterEventHotKey(
        HOTKEY_KEYCODE, HOTKEY_MODS, EventHotKeyID(fourcc("VOCB"), 1), target, 0,
        ctypes.byref(ref))
    _hotkey_refs.extend([handler, ref])
    return status == 0


def serve():
    """常駐模式：視窗預先建好藏著，socket 收到訊息立刻喚醒。

    協定：
      "PRE"   → 觸發端（BTT）即將送出 ⌘C，daemon 監看剪貼簿一變就開窗（最快路徑）
      其他文字 → 直接當單字開窗（服務選單/CLI 路徑）
    """
    import socket
    import threading
    import time as _time

    win = VocabWindow(oneshot=False)
    win.root.withdraw()

    def preload():  # 把慢的初始化都在啟動時做掉：zhconv、語音偵測、字典連線
        to_traditional("預載")
        get_english_voice()
        local_lookup("preload")

    threading.Thread(target=preload, daemon=True).start()
    _start_audio_keepalive()  # 常駐握住音訊通道：發音不再有裝置喚醒截頭/延遲

    try:
        from AppKit import NSPasteboard
        pasteboard = NSPasteboard.generalPasteboard()
    except Exception:
        pasteboard = None

    # 平時每 0.3s 快取 changeCount：PRE 用「手勢前」的值當基準，
    # 就算 BTT 的 ⌘C 比 PRE 訊號先到也不會等白等
    # 記錄剪貼簿「最後一次變化的時間」。BTT 的 ⌘C 可能比 PRE 訊號先完成，
    # 只比對基準值會漏掉；改看變化時間就不怕先後順序。
    clip_state = {
        "count": pasteboard.changeCount() if pasteboard else 0,
        "changed_at": 0.0,
    }

    def tick_clip():
        if pasteboard is not None:
            c = pasteboard.changeCount()
            if c != clip_state["count"]:
                clip_state["count"] = c
                clip_state["changed_at"] = _time.time()
                _dbg(f"clipboard tick: count={c}")
        win.root.after(50, tick_clip)

    tick_clip()

    def show_from_clipboard():
        """等 BTT 的 ⌘C 更新剪貼簿（15ms 輪詢），一變就開窗。"""
        if pasteboard is None:
            win.root.after(200, lambda: win.show(_read_clip()))
            return
        now = _time.time()
        cur = pasteboard.changeCount()
        _dbg(f"trigger: count={cur} tracked={clip_state['count']} "
             f"last_change={now - clip_state['changed_at']:.2f}s ago")
        # ⌘C 已在訊號之前開始（0.6 秒內剛變過）→ 不必再等計數變化，只等內容到齊
        already = cur != clip_state["count"] or now - clip_state["changed_at"] < 0.6
        wait_clip(None if already else cur, now, already)

    def wait_clip(base, now, already=False):
        """等剪貼簿變成 base 以外的值且讀得到文字（最多 1 秒），然後開窗。"""
        deadline = now + 1.0

        def poll():
            # 很多 App 複製是「先清空、再寫入」：計數一跳就讀可能讀到空的，
            # 所以要等到真的讀得到文字才開窗
            if base is None or pasteboard.changeCount() != base:
                text = _read_clip()
                if text.strip():
                    _dbg(f"show after {(_time.time() - now) * 1000:.0f}ms "
                         f"({'already' if already else 'changed'}): {text[:30]!r}")
                    win.show(text)
                    return
            if _time.time() > deadline:
                # ⌘C 沒複製到東西（選取掉了/App 不支援）。不拿舊剪貼簿充數，免得查錯字
                _dbg("timeout: clipboard never changed")
                notify("VocabCloud", "沒有偵測到選取文字，請重新選字再試")
            else:
                win.root.after(15, poll)

        poll()

    def _read_clip():
        try:
            if pasteboard is not None:
                return pasteboard.stringForType_("public.utf8-plain-text") or ""
            return win.root.clipboard_get()
        except Exception:
            return ""

    def listener():
        while True:
            try:
                conn, _ = srv.accept()
                data = conn.recv(4096).decode("utf-8", errors="ignore").strip()
                conn.close()
                if data == "PRE":
                    win.root.after(0, show_from_clipboard)
                elif data == "DISMISS":  # 等同按 Esc（測試用）
                    win.root.after(0, win.dismiss)
                elif data:
                    win.root.after(0, win.show, data)
            except Exception:
                pass

    # 主要訊號：全域快捷鍵（BTT 送 ⌃⌥⇧⌘F19）。socket 的 PRE 保留給舊設定/測試。
    # 快捷鍵回呼發生在 Tk 主迴圈處理事件途中，回呼裡碰 tkinter 會讓直譯器崩潰，
    # 所以回呼只舉旗，由 Tk 自己每 10ms 檢查旗子。
    hotkey_hits = []
    last_trigger = {"t": 0.0}

    def dedupe():
        """原生四指與 BTT 兩條路徑並存時，1 秒內只處理一次。"""
        now = _time.time()
        if now - last_trigger["t"] < 1.0:
            return False
        last_trigger["t"] = now
        return True

    def check_hotkey():
        if hotkey_hits:
            _dbg(f"hotkey x{len(hotkey_hits)}")
            hotkey_hits.clear()
            if dedupe():
                show_from_clipboard()
        win.root.after(10, check_hotkey)

    if _register_hotkey(lambda: hotkey_hits.append(1)):
        check_hotkey()
    else:
        print("hotkey registration failed", file=sys.stderr, flush=True)

    # ---- 主要觸發：daemon 自己偵測四指輕點（MultitouchSupport），不經 BTT ----
    # 觸發後自己送 ⌘C（CGEvent，需給 Python 輔助使用權限），再等剪貼簿開窗。
    tap_hits = []

    def send_cmd_c():
        from Quartz import (CGEventCreateKeyboardEvent, CGEventPost, CGEventSetFlags,
                            kCGEventFlagMaskCommand, kCGHIDEventTap)
        for down in (True, False):
            e = CGEventCreateKeyboardEvent(None, 8, down)  # kVK_ANSI_C
            CGEventSetFlags(e, kCGEventFlagMaskCommand)
            CGEventPost(kCGHIDEventTap, e)

    def on_native_tap():
        if not dedupe():
            return
        now = _time.time()
        _dbg("4-finger tap (native)")
        base = pasteboard.changeCount() if pasteboard is not None else None
        try:
            send_cmd_c()
        except Exception as e:
            _dbg(f"send ⌘C failed: {e}")
            notify("VocabCloud", "無法送出 ⌘C：請到 系統設定 → 隱私權與安全性 → 輔助使用 允許 Python")
            return
        if pasteboard is None:
            win.root.after(200, lambda: win.show(_read_clip()))
        else:
            wait_clip(base, now)

    def check_tap():
        if tap_hits:
            tap_hits.clear()
            on_native_tap()
        win.root.after(10, check_tap)

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from mt_tap import TapDetector
        det = TapDetector(on_tap=lambda: tap_hits.append(1))
        n_dev = det.start()
        _dbg(f"native tap detector: devices={n_dev}")
        if n_dev:
            win._tap_detector = det  # 保住參照
            check_tap()
            try:  # 沒有輔助使用權限就跳系統提示（只會問一次）
                from ApplicationServices import (AXIsProcessTrustedWithOptions,
                                                 kAXTrustedCheckOptionPrompt)
                _dbg(f"accessibility trusted="
                     f"{AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})}")
            except Exception as e:
                _dbg(f"accessibility check failed: {e}")
    except Exception as e:
        _dbg(f"native tap detector failed: {e}")

    if Path(SOCKET_PATH).exists():
        Path(SOCKET_PATH).unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    srv.listen(5)
    threading.Thread(target=listener, daemon=True).start()
    win.root.mainloop()


def run_gui(word):
    """一次性模式（daemon 沒跑時的備援）。"""
    win = VocabWindow(oneshot=True)
    win.root.after(1, win.show, word)
    win.root.mainloop()


def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    if args and args[0] == "--serve":
        serve()
        return
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
        if not send_to_daemon(word):  # daemon 在跑 → 毫秒級；沒跑 → 備援
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
