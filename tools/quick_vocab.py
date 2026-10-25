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
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

POS_ZH = {
    "n": "n. (名詞)", "v": "v. (動詞)", "adj": "adj. (形容詞)", "adv": "adv. (副詞)",
    "prep": "prep. (介系詞)", "conj": "conj. (連接詞)", "pron": "pron. (代名詞)",
    "int": "int. (感嘆詞)",
    "noun": "n. (名詞)", "verb": "v. (動詞)", "adjective": "adj. (形容詞)",
    "adverb": "adv. (副詞)", "preposition": "prep. (介系詞)", "conjunction": "conj. (連接詞)",
    "pronoun": "pron. (代名詞)", "interjection": "int. (感嘆詞)", "abbreviation": "abbr. (縮寫)",
}


def http_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def google_lookup(text, timeout=5):
    """Google 翻譯免金鑰端點，一次拿：翻譯(t)、詞性＋其他譯法(bd)、英文釋義(md)。
    單字與整句都走這裡（長文品質比字典好）。回傳 dict，失敗會丟例外。"""
    q = urllib.parse.quote(text)
    single = " " not in text.strip()
    dts = "&dt=t&dt=bd&dt=md&dt=ex" if single else "&dt=t"  # 整句沒有詞性/釋義/例句，少傳
    data = http_json(
        "https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-TW"
        f"{dts}&q={q}", timeout=timeout)
    out = {"translation": "", "pos": "", "definition": "", "alternatives": [], "example": ""}
    ex = data[13] if len(data) > 13 else None
    if ex and ex[0]:
        out["example"] = re.sub(r"<[^>]+>", "", ex[0][0][0]).strip()
    zh = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0]).strip()
    if zh and zh.lower() != text.strip().lower():
        out["translation"] = zh
    # 詞性/釋義以 md（釋義清單）第一條為準，那才是主要義；bd（譯法清單）的順序不可靠
    # （separate：md 第一條是形容詞，bd 第一條卻是「成套服裝單品」的名詞）
    pos_en = ""
    md = data[12] if len(data) > 12 else None
    if md and md[0] and md[0][1]:
        pos_en = md[0][0] or ""
        out["pos"] = POS_ZH.get(pos_en, pos_en)
        out["definition"] = md[0][1][0][0]
    bd = data[1] if len(data) > 1 else None
    if bd:
        entry = next((e for e in bd if e and e[0] == pos_en), bd[0])
        if not out["pos"] and entry[0]:
            out["pos"] = POS_ZH.get(entry[0], entry[0])
        out["alternatives"] = [t for t in (entry[1] or [])[:5] if t and t != out["translation"]][:4]
    return out


def wiktionary_example(phrase):
    """Wiktionary 的片語條目常附例句（crack down、look forward to…）。約 0.5~1.2s，背景用。"""
    q = urllib.parse.quote(phrase)
    data = http_json(f"https://en.wiktionary.org/api/rest_v1/page/definition/{q}", timeout=6)
    for entry in data.get("en", []):
        for d in entry.get("definitions", []):
            for x in d.get("parsedExamples") or []:
                s = re.sub(r"<[^>]+>", "", x.get("example", "")).strip()
                if s:
                    return s
            for x in d.get("examples") or []:
                s = re.sub(r"<[^>]+>", "", x).strip()
                if s:
                    return s
    return ""


TTS_DIR = DATA_DIR / "tts"


def google_tts_path(text):
    import hashlib
    key = hashlib.md5(text.strip().lower().encode("utf-8")).hexdigest()[:16]
    return TTS_DIR / f"{key}.mp3"


def google_tts(text, timeout=3):
    """Google 翻譯的 TTS 端點（網頁上喇叭的聲音）。抓回 mp3 存快取，回傳路徑。"""
    path = google_tts_path(text)
    if path.exists():
        return path
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    q = urllib.parse.quote(text.strip())
    req = urllib.request.Request(
        f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=en&q={q}",
        headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if not data or len(data) < 500:
        raise RuntimeError("empty tts response")
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


_mp3_proc = None


def _play_mp3(path):
    """afplay 播 mp3（音訊裝置由保溫迴圈維持在喚醒狀態，起播 ~30ms）。重播會先停掉上一個。"""
    global _mp3_proc
    try:
        if _mp3_proc and _mp3_proc.poll() is None:
            _mp3_proc.terminate()
    except Exception:
        pass
    _mp3_proc = subprocess.Popen(["afplay", str(path)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)


def tatoeba_example(word):
    """Tatoeba 例句庫（最後備援；句子偏短）。"""
    q = urllib.parse.quote(word)
    data = http_json(f"https://tatoeba.org/en/api_v0/search?from=eng&query={q}&limit=3&sort=relevance",
                     timeout=6)
    for r in data.get("results", []):
        s = (r.get("text") or "").strip()
        if s and word.lower() in s.lower():
            return s
    return ""


def fetch_example_fallback(word, q):
    """單字例句備援鏈：dictionaryapi → Wiktionary → Tatoeba。都沒有回空字串。"""
    for src in (lambda: fetch_dictapi(q)["example"],
                lambda: wiktionary_example(word),
                lambda: tatoeba_example(word)):
        try:
            ex = src()
        except Exception:
            ex = ""
        if ex:
            return ex
    return ""


def translate_zh(text):
    """英→繁中（Google 的 zh-TW 即台灣正體）。Google 失敗改用 MyMemory；都沒有回空字串。"""
    if not text:
        return ""
    try:
        zh = google_lookup(text)["translation"]
        if zh:
            return zh
    except Exception:
        pass
    try:
        q = urllib.parse.quote(text)
        data = http_json(f"https://api.mymemory.translated.net/get?q={q}&langpair=en|zh-TW")
        zh = data.get("responseData", {}).get("translatedText", "") or ""
        if zh.upper().startswith("MYMEMORY WARNING") or zh.strip().lower() == text.strip().lower():
            return ""
        return zh.strip()
    except Exception:
        return ""


def is_phrase(text):
    """三個字以上或太長 → 當片語/整句處理（只翻譯，不查音標字典）。"""
    return len(text.split()) >= 3 or len(text) > 24


_STOPWORDS = {"a", "an", "the", "is", "are", "was", "were", "be", "been", "it",
              "its", "this", "that", "what", "who", "and", "or", "but", "so",
              "of", "to", "in", "on", "at", "as", "by", "he", "she", "they", "we",
              "his", "her", "their", "my", "your"}


def _word_forms(w):
    """還原常見字尾變化：sets→set、looking→look…（含原形）"""
    forms = [w]
    for suf, rep in (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""),
                     ("ing", "e"), ("ed", ""), ("ed", "e")):
        if w.endswith(suf) and len(w) - len(suf) >= 2:
            forms.append(w[: len(w) - len(suf)] + rep)
    return forms


def datamuse_phrase(phrase):
    """Datamuse 的詞庫含 Wiktionary 片語條目。精確命中且有釋義才算片語，回傳 info 或 None。"""
    q = urllib.parse.quote(phrase)
    res = http_json(f"https://api.datamuse.com/words?sp={q}&qe=sp&md=dr&ipa=1&max=1", timeout=4)
    if not res or res[0].get("word", "").lower() != phrase.lower() or not res[0].get("defs"):
        return None
    pos, _, d = res[0]["defs"][0].partition("\t")
    info = {"word": res[0]["word"], "pos": POS_ZH.get(pos.strip(), pos.strip()),
            "definition": d.strip(), "phonetic": ""}
    for tag in res[0].get("tags", []):
        if tag.startswith("ipa_pron:"):
            info["phonetic"] = f"/{tag.split(':', 1)[1].strip()}/"
            break
    return info


def find_phrases(text, limit=3):
    """在一段選取文字中自動偵測片語（線上，Datamuse，候選並行查，整句約 0.3s）。

    候選：2~4 字的連續組合，以及「動詞 … 介係詞」隔 1~2 個字的組合
    （sets Xiang apart → set apart），第一個字做字尾還原。長的/連續的優先。
    """
    words = [w.lower() for w in re.findall(r"[A-Za-z']+", text)]
    n = len(words)
    cands = []  # (score, phrase)
    for i in range(n):
        for length in (3, 2):  # 4 字片語少見，為了速度不查
            if i + length <= n:
                rest = words[i + 1: i + length]
                for f in _word_forms(words[i]):
                    cands.append((length * 10, " ".join([f] + rest)))
        if words[i] not in _STOPWORDS:  # 隔字動詞片語：動詞 … 介係詞
            for gap in (1, 2):
                j = i + gap + 1
                if j < n and words[j] not in _STOPWORDS - {"to", "on", "in", "at", "by", "of"}:
                    for f in _word_forms(words[i]):
                        cands.append((15, f"{f} {words[j]}"))
    seen, todo = set(), []
    for score, ph in sorted(cands, key=lambda c: -c[0]):
        parts = ph.split()
        if ph in seen or all(p in _STOPWORDS for p in parts) or parts[-1] in {"a", "an", "the"}:
            continue
        seen.add(ph)
        todo.append((score, ph))
    todo = todo[:48]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
        futs = {ex.submit(datamuse_phrase, ph): (score, ph) for score, ph in todo}
        for fut in concurrent.futures.as_completed(futs):
            try:
                info = fut.result()
            except Exception:
                info = None
            if info:
                results.append((futs[fut][0], info))
    results.sort(key=lambda r: -r[0])
    # 同一片語的不同變化形（looking forward to / look forward to）只留最長的一個
    out = []
    for _, info in results:
        if not any(info["word"] in o["word"] or o["word"] in info["word"] for o in out):
            out.append(info)
        if len(out) >= limit:
            break
    return out


_ARPABET = {
    "AA": "ɑ", "AE": "æ", "AH": "ʌ", "AO": "ɔ", "AW": "aʊ", "AY": "aɪ", "EH": "ɛ", "ER": "ɜr",
    "EY": "eɪ", "IH": "ɪ", "IY": "i", "OW": "oʊ", "OY": "ɔɪ", "UH": "ʊ", "UW": "u",
    "B": "b", "CH": "tʃ", "D": "d", "DH": "ð", "F": "f", "G": "ɡ", "HH": "h", "JH": "dʒ",
    "K": "k", "L": "l", "M": "m", "N": "n", "NG": "ŋ", "P": "p", "R": "r", "S": "s",
    "SH": "ʃ", "T": "t", "TH": "θ", "V": "v", "W": "w", "Y": "j", "Z": "z", "ZH": "ʒ",
}


def arpabet_to_ipa(pron):
    """CMU ARPAbet → 常見字典風格 IPA，重音符放在音節開頭（Datamuse 自己轉的會放錯位）。
    'S EH1 P ER0 EY0 T' → 'ˈsɛpəreɪt'"""
    phones = pron.split()
    out, onset = [], []  # onset：上一個母音之後累積的子音，遇到母音時決定重音符要放在它們前面
    for ph in phones:
        m = re.match(r"([A-Z]+)(\d)?$", ph)
        if not m:
            continue
        base, stress = m.group(1), m.group(2)
        if stress is None:  # 子音
            onset.append(_ARPABET.get(base, base.lower()))
            continue
        if base == "AH" and stress == "0":
            sym = "ə"
        elif base == "ER":
            sym = "ər" if stress == "0" else "ɜr"
        else:
            sym = _ARPABET.get(base, base.lower())
        mark = {"1": "ˈ", "2": "ˌ"}.get(stress, "")
        out.append(mark + "".join(onset) + sym)
        onset = []
    out.append("".join(onset))
    return "".join(out)


def fetch_datamuse(q):
    """主要來源：Datamuse（音標/詞性/釋義），穩定。"""
    out = {"phonetic": "", "pos": "", "definition": ""}
    results = http_json(
        f"https://api.datamuse.com/words?sp={q}&qe=sp&md=rd&max=1&ipa=1", timeout=6
    )
    if results:
        r = results[0]
        tags = {t.split(":", 1)[0]: t.split(":", 1)[1].strip() for t in r.get("tags", []) if ":" in t}
        if tags.get("pron"):
            out["phonetic"] = f"/{arpabet_to_ipa(tags['pron'])}/"
        elif tags.get("ipa_pron"):
            out["phonetic"] = f"/{tags['ipa_pron']}/"
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
    """CLI/一次性模式用：Google（翻譯/詞性/釋義）＋ Datamuse（音標）＋ dictionaryapi（例句）並行。"""
    info = {"word": word, "phonetic": "", "pos": "", "definition": "", "example": "",
            "translation": "", "example_zh": "", "audio_url": "", "alternatives": []}
    q = urllib.parse.quote(word)
    single = " " not in word.strip()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f_g = ex.submit(google_lookup, word)
        f_dm = ex.submit(fetch_datamuse, q) if single else None
        f_da = ex.submit(fetch_dictapi, q) if single else None
        try:
            info.update(f_g.result())
        except Exception:
            info["translation"] = translate_zh(word)
        if f_dm:
            try:
                dm = f_dm.result()
                info["phonetic"] = dm["phonetic"]
                if not info["definition"]:
                    info["definition"], info["pos"] = dm["definition"], dm["pos"]
            except Exception:
                pass
        if f_da:
            try:
                da = f_da.result()
                if da["phonetic"]:
                    info["phonetic"] = da["phonetic"]
                info["example"], info["audio_url"] = da["example"], da["audio_url"]
            except Exception:
                pass
    if not single:  # 片語/整句：主條目維持整句，另列出偵測到的片語
        try:
            info["phrases"] = find_phrases(word)
        except Exception:
            info["phrases"] = []
        return info
    if not info["example"]:
        info["example"] = fetch_example_fallback(word, q)
    if info["example"]:
        info["example_zh"] = translate_zh(info["example"])
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
        f"* **釋義：** {info['definition']}{' / ' + info['translation'] if info['definition'] and info['translation'] else info['translation'] if not info['definition'] else ''}\n"
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
        self.google_var = tk.StringVar()
        self.status_var = tk.StringVar()

        # 按鈕與狀態列先 pack 在底部：空間不夠時優先保住按鈕，不會被截掉
        btns = tk.Frame(root)
        btns.pack(side="bottom", anchor="e", pady=(12, 0))
        self.play_btn = tk.Button(btns, text="🔊 發音 (空白鍵)", command=self.on_play)
        self.phrase_btn = tk.Button(btns, text="🧩 片語 (P)", command=self.on_phrase,
                                    state="disabled")
        close_btn = tk.Button(btns, text="關閉 (Esc)", command=self.dismiss)
        self.add_btn = tk.Button(btns, text="➕ 加入單字庫 (fn)", command=self.on_add,
                                 state="disabled", default="active")
        self.play_btn.pack(side="left", padx=4)
        self.phrase_btn.pack(side="left", padx=4)
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
        tk.Label(root, textvariable=self.google_var, font=("Helvetica", 14), fg="#6b7280",
                 wraplength=WRAP, justify="left").pack(anchor="w", pady=(6, 0))

        # 快速鍵主要由 NSEvent 鍵碼監聽處理（不受輸入法影響）；
        # 這些 Tk 綁定只在監聽沒裝起來時當備援，否則會觸發兩次
        self._keys_native = False

        def tk_key(action):
            def h(_e):
                if not self._keys_native:
                    {"add": self.on_add, "play": self.on_play,
                     "dismiss": self.dismiss, "phrase": self.on_phrase}[action]()
            return h

        root.bind("<Escape>", tk_key("dismiss"))
        root.bind("<Return>", tk_key("add"))
        root.bind("a", tk_key("add"))      # 左手無名指原位
        root.bind("A", tk_key("add"))
        root.bind("<space>", tk_key("play"))
        root.bind("p", tk_key("phrase"))
        root.bind("P", tk_key("phrase"))
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
        self.key_hits = []
        state = {"down_at": None, "clean": False}
        try:
            from AppKit import NSEvent

            FN_FLAG = 1 << 23            # NSEventModifierFlagFunction
            KEYCODE_FN = 63              # kVK_Function
            MASK = (1 << 12) | (1 << 10)  # FlagsChanged | KeyDown
            # 視窗內快速鍵改看「硬體鍵碼」：注音等輸入法會把字母鍵攔去組字，
            # Tk 的 "g"/"a" 綁定永遠收不到。鍵碼不經輸入法。
            KEY_ACTIONS = {0: "add", 35: "phrase", 49: "play", 53: "dismiss", 36: "add"}
            MOD_MASK = (1 << 17) | (1 << 18) | (1 << 19) | (1 << 20)  # ⇧⌃⌥⌘

            def handler(event):
                try:
                    if event.type() == 10:  # KeyDown：fn 按住時按了別的鍵 → 是組合鍵
                        state["clean"] = False
                        action = KEY_ACTIONS.get(event.keyCode())
                        if action and not (event.modifierFlags() & MOD_MASK):
                            self.key_hits.append(action)
                            return None  # 吃掉，別再讓 Tk/輸入法處理
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
            self._keys_native = self._fn_monitor is not None
        except Exception as e:
            _dbg(f"fn monitor failed: {e}")
            return
        self._poll_fn()

    def _poll_fn(self):
        if self.fn_hits:
            self.fn_hits.clear()
            if self.root.state() == "normal":
                self.on_add()
        if self.key_hits:
            actions = self.key_hits[:]
            self.key_hits.clear()
            if self.root.state() == "normal":
                for a in actions:
                    {"add": self.on_add, "play": self.on_play,
                     "dismiss": self.dismiss, "phrase": self.on_phrase}[a]()
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
        """語音在獨立子程序（純 NSRunLoop）跑：daemon 的 Tk 主迴圈服務 run loop 太慢，
        同一個合成器在 Tk 裡出聲要 135~370ms，在純 run loop 裡只要 ~60ms。"""
        self.speech = SpeechClient()
        self.speech.ensure()  # 立刻啟動並預熱

    def _speak(self, text, audio_url=""):
        """發音：Google TTS（較自然）。有快取立刻播；沒有就背景抓（~200ms），
        0.9 秒內沒抓到改用常駐 Samantha，抓到的仍存快取供下次用。"""
        import threading

        self._speak_seq = getattr(self, "_speak_seq", 0) + 1
        seq = self._speak_seq
        cached = google_tts_path(text)
        if cached.exists():
            _play_mp3(cached)
            _dbg(f"speak (cached google tts): {text[:20]!r}")
            return
        state = {"spoken": False}
        slock = threading.Lock()

        def fallback():
            with slock:
                if state["spoken"] or seq != self._speak_seq:
                    return
                state["spoken"] = True
            try:
                self.speech.speak(text)
                _dbg(f"speak (samantha fallback): {text[:20]!r}")
            except Exception as e:
                _dbg(f"speech server failed: {e}")
                play_pronunciation(text)

        def fetch():
            try:
                path = google_tts(text)
            except Exception as e:
                _dbg(f"google tts failed: {e}")
                fallback()
                return
            with slock:
                if state["spoken"] or seq != self._speak_seq:
                    return  # 已用 Samantha 唸過，或已換字；mp3 留作快取
                state["spoken"] = True
            _play_mp3(path)
            _dbg(f"speak (google tts): {text[:20]!r}")

        threading.Thread(target=fetch, daemon=True).start()
        self.root.after(900, fallback)

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
        self.base_info = None
        self.phrase_idx = -1  # -1 = 顯示整句；0.. = 第幾個片語卡
        self.pending_add = False
        self.phrase_btn.config(state="disabled")
        self.word_var.set(word)
        self.phon_var.set("")
        self.trans_var.set("查詢中…")
        self.def_var.set("")
        self.ex_var.set("")
        self.google_var.set("")
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
        self._speak(word)  # 先發音再開窗：發音在子程序跑，和開窗同時進行

        self._place()
        self.root.deiconify()
        self._order_front()
        self._reset_hide_timer()
        self._watch_focus()

        empty = {"word": word, "phonetic": "", "pos": "", "definition": "",
                 "example": "", "translation": "", "example_zh": "", "audio_url": "",
                 "alternatives": []}

        cur = dict(empty)
        lock = threading.Lock()

        def merge(**fields):  # 各背景來源把結果合併進同一份 info，再整份送去顯示
            with lock:
                cur.update(fields)
                snap = dict(cur)
            self._post_fill(snap, gen)

        def worker():
            # 主要來源：Google（翻譯/詞性/英文釋義/其他譯法），單字與整句都走這裡，~0.1s
            try:
                merge(**google_lookup(word))
            except Exception as e:
                _dbg(f"google lookup failed: {e}")
                merge(translation=translate_zh(word))  # MyMemory 備援
            if " " in word.strip():
                # 片語/整句：主條目維持整句；背景偵測句中的片語（Datamuse，~0.3s）
                # 只列出來，由使用者按 P 決定要不要切成片語卡
                try:
                    matches = find_phrases(word)
                except Exception as e:
                    _dbg(f"phrase detect failed: {e}")
                    matches = []
                if matches:
                    _dbg(f"phrases: {[m['word'] for m in matches]}")
                merge(phrases=matches, complete=True)
                return

            q = urllib.parse.quote(word)
            # 三個背景來源都結束才標記 complete：練習系統需要音標/詞性/釋義/例句/翻譯齊全，
            # 「加入」會等到 complete 才真的寫入
            left = {"n": 3}

            def done():
                with lock:
                    left["n"] -= 1
                    last = left["n"] == 0
                if last:
                    merge(complete=True)

            def phon():  # 音標（Datamuse，穩定）
                try:
                    dm = fetch_datamuse(q)
                    fields = {"phonetic": dm["phonetic"]} if dm["phonetic"] else {}
                    with lock:
                        if not cur["definition"] and dm["definition"]:
                            fields.update(definition=dm["definition"], pos=dm["pos"])
                    if fields:
                        merge(**fields)
                except Exception:
                    pass
                finally:
                    done()

            def example():  # 例句：Google 有就只翻譯；沒有走備援鏈 dictionaryapi→Wiktionary→Tatoeba
                try:
                    with lock:
                        ex = cur.get("example", "")
                    if not ex:
                        ex = fetch_example_fallback(word, q)
                    if ex:
                        merge(example=ex, example_zh=translate_zh(ex))
                    else:
                        _dbg(f"no example found for {word!r}")
                finally:
                    done()

            def extra():  # 較標準的音標（dictionaryapi.dev，常掛就算了；發音已改用 Google TTS）
                try:
                    da = fetch_dictapi(q)
                    if da["phonetic"]:
                        merge(phonetic=da["phonetic"])
                except Exception:
                    pass
                finally:
                    done()

            threading.Thread(target=example, daemon=True).start()
            threading.Thread(target=phon, daemon=True).start()
            threading.Thread(target=extra, daemon=True).start()

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
        if not info.get("is_phrase_card"):
            self.base_info = info  # 整句的資料，按 P 切片語卡後還能切回來
        if info["word"] != self.word_var.get():  # 切片語卡 / 切回整句
            self.word_var.set(info["word"])
            self.word_label.config(
                font=("Helvetica", 16 if is_phrase(info["word"]) else 30, "bold")
            )
        self.phon_var.set(info.get("phonetic") or "")
        self.trans_var.set(info["translation"] or "（查無翻譯）")
        parts = []
        if info["pos"]:
            parts.append(f"[{info['pos']}]")
        if info.get("definition"):
            # 視窗只顯示英文釋義；中文翻譯上面綠字已有，不重複（存進字庫仍是完整版）
            parts.append(info["definition"].split(" / ")[0])
        self.def_var.set(" ".join(parts))
        ex = info.get("example") or ""
        if ex and info.get("example_zh"):
            ex += f"\n{info['example_zh']}"
        self.ex_var.set(ex)
        phrases = info.get("phrases") or []
        if info.get("alternatives"):
            self.google_var.set("其他譯法：" + "、".join(info["alternatives"]))
        elif phrases:
            names = "、".join(m["word"] for m in phrases)
            if info.get("is_phrase_card"):
                self.google_var.set(f"🧩 片語 {self.phrase_idx + 1}/{len(phrases)}：{names}（P 切換／回整句）")
            else:
                self.google_var.set(f"🧩 偵測到片語：{names}（按 P 切成片語卡）")
        else:
            self.google_var.set("")
        self.phrase_btn.config(state="normal" if phrases else "disabled")
        self.add_btn.config(state="normal")
        self._place()  # 內容變多視窗會長高，重新調整避免超出螢幕
        if self.pending_add and info.get("complete"):  # 資料齊了 → 完成先前按下的加入
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
    def _current_word(self):
        return self.info["word"] if self.info else self.word

    def on_phrase(self):
        """P：在「整句」和偵測到的各片語卡之間輪流切換。
        片語卡 = 片語當主條目（詞性/釋義/音標來自 Datamuse，中譯向 Google 查），
        原句變例句；fn 加入的就是當下顯示的這張。"""
        base = self.base_info
        phrases = (base or {}).get("phrases") or []
        if not phrases:
            return
        self.phrase_idx += 1
        if self.phrase_idx >= len(phrases):
            self.phrase_idx = -1
            self.fill(base, self.gen)  # 回到整句
            return
        m = phrases[self.phrase_idx]
        _dbg(f"phrase card {self.phrase_idx + 1}/{len(phrases)}: {m['word']!r}")
        # 你選的若是含上下文的句子，就拿它當例句；若只選了片語本身，例句另外向 Wiktionary 要
        has_context = len(base["word"].split()) >= len(m["word"].split()) + 2
        card = {**base, **m, "is_phrase_card": True, "translation": "", "complete": False,
                "example": base["word"] if has_context else "",
                "example_zh": base.get("translation", "") if has_context else "",
                "alternatives": [], "audio_url": ""}
        self.fill(card, self.gen)
        self._speak(m["word"])
        gen = self.gen
        state = {"card": card, "left": 1 if has_context else 2}
        import threading
        ulock = threading.Lock()

        def update(**fields):
            with ulock:
                if self.info is not state["card"]:  # 已切到別張卡
                    return
                state["card"] = {**state["card"], **fields}
                snap = state["card"]
            self._post_fill(snap, gen)

        def done():
            with ulock:
                state["left"] -= 1
                last = state["left"] == 0
            if last:
                update(complete=True)

        def fetch_zh():  # 片語中譯（~60ms）
            try:
                zh = google_lookup(m["word"])["translation"]
                if zh:
                    update(translation=zh)
            except Exception:
                pass
            finally:
                done()

        def fetch_example():  # 片語例句（Wiktionary → Tatoeba）＋ 中譯
            try:
                ex = ""
                for src in (lambda: wiktionary_example(m["word"]), lambda: tatoeba_example(m["word"])):
                    try:
                        ex = src()
                    except Exception:
                        ex = ""
                    if ex:
                        break
                if ex:
                    update(example=ex, example_zh=translate_zh(ex))
            finally:
                done()

        threading.Thread(target=fetch_zh, daemon=True).start()
        if not has_context:
            threading.Thread(target=fetch_example, daemon=True).start()

    def on_play(self):
        self._speak(self._current_word(), self.info.get("audio_url", "") if self.info else "")

    def on_add(self):
        if not self.info or not self.info.get("complete"):
            # 資料還沒齊（例句/翻譯還在抓）：立刻關窗，齊了在背景寫入；
            # 最多等 8 秒，超過就用現有資料寫入，不讓它卡住
            if not self.pending_add:
                self.pending_add = True
                gen = self.gen

                def force():
                    if self.pending_add and self.gen == gen and self.info:
                        _dbg("add: sources slow, writing what we have")
                        self.pending_add = False
                        self._write_card(self.info)

                self.root.after(8000, force)
            if self.oneshot:  # 一次性模式關窗=程式結束，只能留窗等
                self.status_label.config(fg="#6b7280")
                self.status_var.set("⏳ 查詢中，完成後自動加入…")
            else:
                self.dismiss()  # daemon 模式：視窗先收，結果看系統通知
            return
        self._write_card(self.info)

    def _write_card(self, info):
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


def _disable_app_nap(reason):
    """宣告延遲敏感活動，禁止 App Nap。沒有視窗的背景程序（語音子程序、
    視窗收起時的 daemon）會被節流，run loop 喚醒變慢，發音延遲暴增到 300ms。
    回傳的 activity 物件必須保住，釋放即解除。"""
    try:
        from Foundation import NSProcessInfo
        USER_INITIATED_ALLOWING_IDLE_SLEEP = 0x00FFFFFF & ~(1 << 20)
        LATENCY_CRITICAL = 0xFF00000000
        return NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            USER_INITIATED_ALLOWING_IDLE_SLEEP | LATENCY_CRITICAL, reason)
    except Exception as e:
        _dbg(f"app nap disable failed: {e}")
        return None


ENABLED_FILE = DATA_DIR / "enabled"   # 內容 "0" = 停用；不存在 = 啟用


def load_enabled():
    try:
        return ENABLED_FILE.read_text().strip() != "0"
    except OSError:
        return True


def save_enabled(flag):
    try:
        ENABLED_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENABLED_FILE.write_text("1" if flag else "0")
    except OSError:
        pass


def _setup_status_item(state):
    """選單列圖示：啟用/停用切換、開單字檔、看紀錄、重啟、結束。
    回呼在 AppKit 事件中執行，裡面絕不碰 tkinter（會讓直譯器崩潰）；
    只改 state 與 AppKit 物件，由 Tk 的輪詢讀 state。"""
    try:
        from AppKit import (NSImage, NSMenu, NSMenuItem, NSObject, NSStatusBar,
                            NSVariableStatusItemLength)
    except Exception as e:
        _dbg(f"status item unavailable: {e}")
        return None

    class Controller(NSObject):
        def refresh(self):
            on = state["enabled"]
            try:
                # 自訂圖示（tools/make_icons.py 產生）：雲裡鏤空 A；停用為空心雲
                icon = DATA_DIR.parent / "icons" / f"menubar_{'on' if on else 'off'}@2x.png"
                img = NSImage.alloc().initWithContentsOfFile_(str(icon)) if icon.exists() else None
                if img is None:  # 沒有圖檔就退回系統符號
                    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                        "character.book.closed.fill" if on else "book.closed", "VocabCloud")
                else:
                    img.setSize_((18, 18))
                img.setTemplate_(True)  # 單色模板：隨深淺色選單列自動反轉
                self.item.button().setImage_(img)
                self.item.button().setAppearsDisabled_(not on)
            except Exception:
                self.item.button().setTitle_("📖" if on else "📕")
            # 標準勾選框語意：文字固定，打勾 = 目前啟用；點一下切換
            self.toggle_item.setState_(1 if on else 0)

        def toggle_(self, sender):
            state["enabled"] = not state["enabled"]
            save_enabled(state["enabled"])
            state["restart_detector"] = True  # 由 Tk 輪詢執行（這裡不能碰 tkinter）
            _dbg(f"enabled={state['enabled']}")
            self.refresh()
            notify("VocabCloud", "查單字已啟用" if state["enabled"] else "查單字已停用（選單列可重新啟用）")

        def openVocab_(self, sender):
            today = VOCAB_LOG_DIR / f"vocab_{date.today().isoformat()}.md"
            subprocess.Popen(["open", str(today if today.exists() else VOCAB_LOG_DIR)])

        def openLog_(self, sender):
            subprocess.Popen(["open", "-a", "Console", "/tmp/vocabcloud-daemon.log"])

        def restart_(self, sender):
            _dbg("restart requested from menu")
            os._exit(0)  # launchd KeepAlive 會立刻重新拉起

        def quit_(self, sender):
            _dbg("quit requested from menu")
            subprocess.Popen(["launchctl", "bootout",
                              f"gui/{os.getuid()}/com.vocabcloud.quickvocab"])

    ctl = Controller.alloc().init()
    ctl.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    menu = NSMenu.alloc().init()

    def add(title, action, key=""):
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if action:
            it.setTarget_(ctl)
        menu.addItem_(it)
        return it

    hdr = add("VocabCloud 查單字", None)
    hdr.setEnabled_(False)
    menu.addItem_(NSMenuItem.separatorItem())
    ctl.toggle_item = add("啟用查單字", "toggle:")
    menu.addItem_(NSMenuItem.separatorItem())
    add("開啟今天的單字檔", "openVocab:")
    add("查看紀錄", "openLog:")
    menu.addItem_(NSMenuItem.separatorItem())
    add("重新啟動", "restart:")
    add("結束（下次登入會自動回來）", "quit:")
    ctl.item.setMenu_(menu)
    ctl.refresh()
    return ctl


class SpeechClient:
    """把要唸的字寫進語音子程序的 stdin（微秒級）；子程序掛了就重開。"""

    def __init__(self):
        self.proc = None

    def ensure(self):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--speech-server"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=sys.stderr,
            )

    def speak(self, text):
        self.ensure()
        self.proc.stdin.write((text.replace("\n", " ").strip() + "\n").encode("utf-8"))
        self.proc.stdin.flush()


def speech_server():
    """常駐語音子程序：stdin 每行一個要唸的字（"STOP" 停止）。
    純 NSRunLoop，不受 Tk 拖累；啟動時音量 0 唸真字預載語音，每 30 秒保溫。"""
    import queue
    import threading
    from AppKit import NSDate, NSObject, NSRunLoop, NSSpeechSynthesizer

    class Timer(NSObject):
        t0 = 0.0
        first = None

        def speechSynthesizer_willSpeakWord_ofString_(self, synth, rng, text):
            if self.first is None and self.t0:
                self.first = time.time() - self.t0
                _dbg(f"speech started after {self.first * 1000:.0f}ms")

    voice = None
    for v in NSSpeechSynthesizer.availableVoices():
        sv = str(v)
        if "en-US" in sv or ".en_US." in sv:
            voice = v
            if "Samantha" in sv:
                break
    synth = NSSpeechSynthesizer.alloc().initWithVoice_(voice)
    timer = Timer.alloc().init()
    synth.setDelegate_(timer)
    _activity = _disable_app_nap("VocabCloud speech")  # noqa: F841 保住參照

    q = queue.Queue()

    def reader():
        for line in sys.stdin:
            q.put(line.rstrip("\n"))
        q.put(None)  # 父程序關了

    threading.Thread(target=reader, daemon=True).start()

    def warm():
        if not synth.isSpeaking():
            synth.setVolume_(0.0)
            synth.startSpeakingString_("warm up")

    warm()
    last_warm = time.time()
    loop = NSRunLoop.currentRunLoop()
    while True:
        loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.005))
        try:
            item = q.get_nowait()
        except queue.Empty:
            item = ""
        if item is None:
            return
        if item == "STOP":
            synth.stopSpeaking()
        elif item:
            synth.stopSpeaking()
            synth.setVolume_(1.0)
            timer.t0, timer.first = time.time(), None
            synth.startSpeakingString_(item)
        if time.time() - last_warm > 30:
            warm()
            last_warm = time.time()


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
        wav = DATA_DIR / "silence.wav"
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
    win._activity = _disable_app_nap("VocabCloud daemon")  # 視窗收起時也不被節流
    state = {"enabled": load_enabled()}
    win._status = _setup_status_item(state)  # 保住參照
    _dbg(f"enabled={state['enabled']}")

    def preload():  # 把慢的初始化都在啟動時做掉：zhconv、語音偵測、字典連線
        get_english_voice()
        try:
            google_lookup("hello")  # 暖連線（DNS/TLS），第一次查字才不會多等 1 秒
        except Exception:
            pass

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
                elif data == "TAP":      # 等同四指輕點（測試用）
                    tap_hits.append(1)
                elif data == "WAKE":     # 模擬喚醒通知（測試用）
                    wake_hits.append("socket WAKE")
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
            if state["enabled"] and dedupe():
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

    ax_state = {"trusted": False, "nagged_at": 0.0}

    def ax_trusted():
        try:
            from ApplicationServices import AXIsProcessTrusted
            return bool(AXIsProcessTrusted())
        except Exception:
            return True  # 查不到就當有，讓 ⌘C 試試看

    def watch_ax():  # 每 3 秒看一次；使用者一給權限就生效，不必重啟 daemon
        t = ax_trusted()
        if t != ax_state["trusted"]:
            ax_state["trusted"] = t
            _dbg(f"accessibility trusted={t}")
            if t:
                notify("VocabCloud", "已取得輔助使用權限，四指輕點可以用了")
        win.root.after(3000, watch_ax)

    def ax_selected_text():
        """用輔助使用 API 直接讀前景 App 的選取文字（約 1~5ms，不碰剪貼簿）。
        不支援的 App（部分 Electron/網頁元件）會回傳空字串，呼叫端退回 ⌘C。"""
        try:
            from ApplicationServices import (
                AXUIElementCopyAttributeValue, AXUIElementCreateSystemWide,
                kAXFocusedApplicationAttribute, kAXFocusedUIElementAttribute,
                kAXSelectedTextAttribute)
            err, app = AXUIElementCopyAttributeValue(
                AXUIElementCreateSystemWide(), kAXFocusedApplicationAttribute, None)
            if err or app is None:
                return ""
            err, el = AXUIElementCopyAttributeValue(app, kAXFocusedUIElementAttribute, None)
            if err or el is None:
                return ""
            err, text = AXUIElementCopyAttributeValue(el, kAXSelectedTextAttribute, None)
            return str(text) if not err and text else ""
        except Exception as e:
            _dbg(f"AX read failed: {e}")
            return ""

    def on_native_tap():
        if not dedupe():
            return
        now = _time.time()
        _dbg("4-finger tap (native)")
        text = ax_selected_text()
        if text.strip():
            _dbg(f"AX selected text in {(_time.time() - now) * 1000:.0f}ms: {text[:30]!r}")
            win.show(text)
            return
        _dbg("AX gave nothing → fallback ⌘C")
        if not ax_state["trusted"] and not ax_trusted():
            # 沒權限時 ⌘C 會被系統默默丟掉；直接說清楚並打開設定頁，不要誤報「沒選到字」
            if now - ax_state["nagged_at"] > 20:
                ax_state["nagged_at"] = now
                notify("VocabCloud", "Python 還沒有「輔助使用」權限，無法送出 ⌘C。"
                                     "請在設定頁把 Python 打開")
                subprocess.Popen(["open", "x-apple.systempreferences:"
                                  "com.apple.preference.security?Privacy_Accessibility"])
            return
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
        if state.pop("restart_detector", False):
            restart_detector("toggle")
        if tap_hits:
            tap_hits.clear()
            if state["enabled"]:
                on_native_tap()
        win.root.after(10, check_tap)

    wake_hits = []

    def restart_detector(reason):
        """睡眠/螢幕喚醒後 MultitouchSupport 的回呼會失效，整個重新註冊。"""
        det = getattr(win, "_tap_detector", None)
        if det is None:
            return
        try:
            det.stop()
            n = det.start()
            _dbg(f"tap detector restarted ({reason}): devices={n}")
        except Exception as e:
            _dbg(f"tap detector restart failed ({reason}): {e}")

    def check_wake():
        if wake_hits:
            reason = wake_hits[-1]
            wake_hits.clear()
            win.root.after(1500, lambda: restart_detector(reason))  # 等裝置重新列舉好
        win.root.after(500, check_wake)

    def _watch_wake():
        """NSWorkspace 喚醒通知。回呼只舉旗（不碰 tkinter），由 Tk 輪詢處理。"""
        try:
            from AppKit import NSObject, NSWorkspace
            from Foundation import NSNotificationCenter  # noqa: F401

            class WakeObserver(NSObject):
                def wake_(self, note):
                    wake_hits.append(str(note.name()))

            obs = WakeObserver.alloc().init()
            nc = NSWorkspace.sharedWorkspace().notificationCenter()
            for name in ("NSWorkspaceDidWakeNotification", "NSWorkspaceScreensDidWakeNotification",
                         "NSWorkspaceSessionDidBecomeActiveNotification"):
                nc.addObserver_selector_name_object_(obs, "wake:", name, None)
            win._wake_observer = obs
            check_wake()
        except Exception as e:
            _dbg(f"wake watcher failed: {e}")

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from mt_tap import TapDetector
        det = TapDetector(on_tap=lambda: tap_hits.append(1))
        n_dev = det.start()
        _dbg(f"native tap detector: devices={n_dev}")
        if n_dev:
            win._tap_detector = det  # 保住參照
            win._restart_detector = restart_detector
            check_tap()
            _watch_wake()
            try:  # 沒有輔助使用權限就跳系統提示（系統只會問一次，之後要手動到設定頁開）
                from ApplicationServices import (AXIsProcessTrustedWithOptions,
                                                 kAXTrustedCheckOptionPrompt)
                ax_state["trusted"] = bool(
                    AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))
            except Exception as e:
                _dbg(f"accessibility check failed: {e}")
            _dbg(f"accessibility trusted={ax_state['trusted']}")
            win.root.after(3000, watch_ax)
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
    if args and args[0] == "--speech-server":
        speech_server()
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
