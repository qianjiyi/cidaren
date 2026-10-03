"""
词达人 (vocabgo) 全自动答题脚本 v3
支持 mode=0/11/31/32 全题型, 匹配不上时用 LLM 兜底
"""

import base64, hashlib, json, os, random, re, time, requests, uuid, certifi
from contextlib import contextmanager

try:
    from .config import get_missing_auth_fields, get_runtime_config
except ImportError:  # pragma: no cover
    from config import get_missing_auth_fields, get_runtime_config

SALT = "ajfajfamsnfaflfasakljdlalkflak"
VERSION = "2.7.0.260507_01"
BASE = "https://app.vocabgo.com/studentv1/api"
STUDENT_BASE = "https://app.vocabgo.com/student/api"
BANK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bank.json")
WORD_PAT = re.compile(r'\{(\w+)\}')

JV = {
    "2_1254":  [{"s":0,"n":3},{"s":1,"n":2},{"s":31,"n":1},{"s":41,"n":2},{"s":51,"n":1},{"s":87,"n":1},{"s":97,"n":1}],
    "2_10234": [{"s":0,"n":3},{"s":1,"n":4},{"s":39,"n":1},{"s":57,"n":2},{"s":188,"n":1},{"s":259,"n":1},{"s":316,"n":2}],
    "2_9214":  [{"s":0,"n":3},{"s":1,"n":4},{"s":41,"n":2},{"s":57,"n":1},{"s":139,"n":2},{"s":272,"n":1},{"s":361,"n":2}],
    "2_9314":  [{"s":0,"n":3},{"s":1,"n":4},{"s":31,"n":2},{"s":60,"n":1},{"s":152,"n":2},{"s":256,"n":1}],
    "3_1021":  {"uc":[{"s":0,"n":1},{"s":1,"n":2},{"s":33,"n":1},{"s":57,"n":1},{"s":111,"n":1}],"avg":5,"loc":[1,3,2,0,4]},
    "3_2265":  {"uc":[{"s":0,"n":2},{"s":1,"n":3},{"s":33,"n":1},{"s":57,"n":1},{"s":121,"n":1}],"avg":5,"loc":[3,1,0,4,2]},
    "3_2277":  {"uc":[{"s":0,"n":3},{"s":1,"n":3},{"s":32,"n":2},{"s":50,"n":1},{"s":110,"n":1}],"avg":5,"loc":[3,1,0,4,2]},
}

# ── 加解密 ──
def _md5(s): return hashlib.md5(s.encode()).hexdigest()

def _sign(params: dict) -> str:
    parts = []
    for k in sorted(params):
        v = params[k]
        if isinstance(v, (dict, list)):
            v = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
        if v or v == 0:
            parts.append(f"{k}={v}")
    return _md5("&".join(parts) + SALT)

def _b64d(s: str) -> str:
    s = s.strip().replace(" ", ""); s += "=" * (-len(s) % 4)
    return base64.b64decode(s).decode()

def _pluck(d: str, rules) -> str:
    for r in rules:
        s, n = r["s"], r["n"]; d = (d[:s] if s else "") + d[s + n:]
    return d

def _decrypt(resp: dict) -> dict:
    jv = str(resp.get("jv", ""))
    data = resp.get("data")
    if not jv or jv == "0" or not isinstance(data, str): return resp
    if jv == "1":
        resp["data"] = json.loads(_b64d(data[32:]))
    elif jv.startswith("2_") and jv in JV:
        resp["data"] = json.loads(_b64d(_pluck(data, JV[jv])))
    elif jv.startswith("3_") and jv in JV:
        cfg = JV[jv]; d = _pluck(data, cfg["uc"])
        avg, loc = cfg["avg"], cfg["loc"]; chunk = len(d) // avg
        pieces = [d[i*chunk:(i+1)*chunk] for i in range(avg)]
        out = "".join(pieces[loc.index(i)] for i in range(avg))
        if len(d) % chunk: out += d[avg*chunk:]
        resp["data"] = json.loads(_b64d(out))
    return resp

def _ms(): return int(time.time() * 1000)
def _sleep(lo=1.5, hi=3.5): time.sleep(random.uniform(lo, hi))
def _norm(s): return re.sub(r'\s+', '', s).lower().strip()

# ── 题库 ──
def _bank_load():
    if os.path.exists(BANK_FILE):
        try: return json.load(open(BANK_FILE, encoding="utf-8"))
        except: pass
    return {}

@contextmanager
def _bank_lock(lock_path):
    with open(lock_path, "a+b") as lf:
        if os.name == "nt":
            import msvcrt
            lf.seek(0)
            msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lf.seek(0)
                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

def _bank_save(bank):
    """多进程安全: 用 pid 区分 tmp + 文件锁串行化合并"""
    lock_path = BANK_FILE + ".lock"
    tmp_path = f"{BANK_FILE}.{os.getpid()}.tmp"
    with _bank_lock(lock_path):
        try:
            # 锁内重新加载, 与磁盘最新状态合并 (防止覆盖其他进程的写入)
            disk = {}
            if os.path.exists(BANK_FILE):
                try: disk = json.load(open(BANK_FILE, encoding="utf-8"))
                except: pass
            disk.update(bank)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(disk, f, ensure_ascii=False, indent=1)
            os.replace(tmp_path, BANK_FILE)
        finally:
            try:
                if os.path.exists(tmp_path): os.remove(tmp_path)
            except: pass

def _chat_completions_url(llm_url: str) -> str:
    base = llm_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"

def _topic_key(t):
    mode = t.get("topic_mode", "?")
    stem = (t.get("stem") or {}).get("content", "")
    remark = (t.get("stem") or {}).get("remark", "") or ""
    if isinstance(remark, list):
        remark = json.dumps(remark, ensure_ascii=False, sort_keys=True)
    opts = "|".join((o.get("content") or "")[:20] for o in (t.get("options") or []))
    sn = re.sub(r'\s+', ' ', stem).strip().lower()
    kind = "coll" if _is_collocation(t) else "norm"
    return f"{mode}::{kind}::{sn}::{remark}::{opts.lower()}"

def _is_collocation(topic):
    """搭配题: mode=31 但 stem.remark 是 list (含 relation 字段)"""
    if topic.get("topic_mode") != 31:
        return False
    rk = (topic.get("stem") or {}).get("remark")
    return isinstance(rk, list) and len(rk) > 0 and isinstance(rk[0], dict)

def _llm_answer(topic, word_defs):
    runtime = get_runtime_config()
    llm_url = (runtime.get("LLM_URL") or "").strip()
    llm_key = (runtime.get("LLM_KEY") or "").strip()
    llm_model = (runtime.get("LLM_MODEL") or "step-3.6").strip() or "step-3.6"

    if not llm_url or not llm_key:
        return None
    mode = topic.get("topic_mode")
    stem_obj = topic.get("stem") or {}
    stem = stem_obj.get("content", "")
    remark = stem_obj.get("remark", "") or ""
    opts = topic.get("options") or []
    opts_str = "\n".join(f"{i}. {o.get('content','')}" for i, o in enumerate(opts))

    # 构建 word_defs 上下文
    wd_str = ""
    if word_defs:
        wd_lines = [f"  {w}: {'; '.join(ds)}" for w, ds in list(word_defs.items())[:40]]
        wd_str = "已知单词释义:\n" + "\n".join(wd_lines) + "\n\n"

    if mode == 32:
        prompt = f"""{wd_str}题目: 用选项中的词组成短语, 中文含义是「{remark}」
空格数: {stem}
选项:
{opts_str}

请选出正确的词并按正确顺序排列。只回答逗号分隔的选项内容(如: in,many,instances), 不要其他文字。"""
    elif mode == 31:
        prompt = f"""{wd_str}题目: 以下哪个是单词「{stem}」的正确释义?
选项:
{opts_str}

只回答选项编号(如: 0), 不要其他文字。"""
    elif ("{}" in stem or "_" in stem) and not opts:
        prompt = f"""{wd_str}题目: 根据中文提示补全英文短语
题干: {stem}
中文提示: {remark}

只回答空格处应填的英文内容；如果有多个空格，用逗号分隔。不要解释。"""
    elif mode == 11:
        rk = f"句子中文翻译(参考): {remark}\n" if remark else ""
        prompt = f"""{wd_str}题目: 根据句意选择句中 {{}} 内单词的正确释义
句子: {stem}
{rk}选项:
{opts_str}

注意: 单词常有多个词义和词性, 必须结合上下文和中文翻译判断该词在此句中的具体含义。
只回答选项编号(如: 0), 不要其他文字。"""
    else:
        prompt = f"""{wd_str}题目 (mode={mode}):
题干: {stem}
备注: {remark}
选项:
{opts_str}

选择正确答案。如果是选择题回答编号(如: 0); 如果是组词题回答逗号分隔的词(如: in,many,instances)。不要其他文字。"""

    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {llm_key}",
            "X-LLM-TAG": "data_annotation"
        }
        data = {
            "model": llm_model,
            "messages": [
                {"role": "system", "content": f"[{uuid.uuid4()}] 你是英语词汇专家。请精准回答，只输出答案，不要解释。"},
                {"role": "user", "content": prompt}
            ]
        }
        url = _chat_completions_url(llm_url)
        resp = None
        last_err = None
        for attempt in range(3):
            try:
                resp = requests.post(url, headers=headers, json=data, timeout=(10, 60))
                resp.raise_for_status()
                break
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = e
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                print(f"    LLM error: {e} (重试 {attempt+1} 次后失败)")
                return None
        if resp is None:
            print(f"    LLM error: {last_err}")
            return None
        if not resp.text.strip():
            print(f"    LLM error: 空响应 (status={resp.status_code})")
            return None
        try:
            rj = resp.json()
        except ValueError:
            print(f"    LLM error: 非 JSON 响应 (status={resp.status_code}): {resp.text[:200]}")
            return None
        if "choices" not in rj or not rj["choices"]:
            print(f"    LLM error: 响应无 choices: {rj}")
            return None
        ans_text = rj["choices"][0]["message"]["content"].strip()

        if mode == 32:
            numeric_parts = [p for p in re.split(r'[,，\s]+', ans_text.strip()) if p]
            if numeric_parts and all(re.fullmatch(r'\d+', p) for p in numeric_parts):
                words = []
                for p in numeric_parts:
                    idx = int(p)
                    if 0 <= idx < len(opts):
                        words.append(opts[idx].get("content", ""))
                    else:
                        words = []
                        break
                if words and all(words):
                    return ",".join(words)
            return ans_text
        else:
            m = re.search(r'\d+', ans_text)
            if m:
                return int(m.group())
            ans_norm = _norm(ans_text)
            for i, opt in enumerate(opts):
                content = opt.get("content", "")
                opt_norm = _norm(content)
                if ans_norm and (ans_norm == opt_norm or ans_norm in opt_norm or opt_norm in ans_norm):
                    return opt.get("answer_tag", i)
            if ("{}" in stem or "_" in stem) and not opts and ans_text:
                return ans_text
    except Exception as e:
        print(f"    LLM error: {e}")
    return None

# ── 答案匹配 ──
def _match_answer(topic, word_defs):
    stem_obj = topic.get("stem") or {}
    stem = stem_obj.get("content", "")
    remark = stem_obj.get("remark", "") or ""
    opts = topic.get("options") or []
    mode = topic.get("topic_mode")

    # 搭配题: stem.remark 是 list, 取所有 relation 直接命中选项
    if _is_collocation(topic):
        return _match_collocation(remark, opts)

    # mode=32: 组词题 — 用 remark(中文) + word_defs 匹配
    if mode == 32:
        return _match_mode32(stem, remark, opts, word_defs)

    # mode=11: 句子带 {word} → 一词多义, 必须靠上下文, 直接交给 LLM
    if mode == 11:
        return None

    # mode=31 或其他选择题: stem 是单词, 选项是释义
    if mode in (31, None) or (mode and mode != 0):
        word = re.sub(r'\s+', '', stem).lower()
        if word and not word.startswith('_'):
            return _match_word_to_def(word, opts, word_defs)

    return None

def _match_collocation(remark_list, opts):
    """搭配题: remark 里所有 relation 字段就是答案词, 返回所有匹配的 answer_tag 列表"""
    if not isinstance(remark_list, list):
        return None
    relations = set()
    for r in remark_list:
        if isinstance(r, dict):
            rel = (r.get("relation") or "").strip().lower()
            if rel:
                relations.add(rel)
    if not relations:
        return None
    tags = []
    for opt in opts:
        c = (opt.get("content") or "").strip().lower()
        if c in relations:
            tags.append(opt.get("answer_tag"))
    return tags if tags else None

def _match_word_to_def(target, opts, word_defs):
    defs = word_defs.get(target)
    if not defs:
        for bw in word_defs:
            if target.startswith(bw) or bw.startswith(target):
                defs = word_defs[bw]; break
    if not defs:
        return None

    defs_norm = [_norm(d) for d in defs]
    for i, opt in enumerate(opts):
        ot = _norm(opt.get("content", ""))
        for dn in defs_norm:
            if ot == dn or dn in ot or ot in dn:
                return opt.get("answer_tag", i)

    for i, opt in enumerate(opts):
        ot = opt.get("content", "")
        for d in defs:
            core = re.sub(r'^[a-z]+\.\s*', '', d)
            kws = [kw.strip() for kw in re.split(r'[；;，,]', core) if len(kw.strip()) >= 2]
            for kw in kws:
                if kw in ot:
                    return opt.get("answer_tag", i)
    return None

def _match_mode32(stem, remark, opts, word_defs):
    if not remark: return None
    opt_words = [o.get("content", "") for o in opts]
    n_blanks = stem.count('_')

    # 从 word_defs 里找: 哪个 word 的某个释义包含 remark 关键词?
    # 然后尝试用该 word 的变形 + 常见搭配组成短语
    # 这个比较难自动化, 交给 LLM 处理
    return None

# ── 客户端 ──
class Client:
    def __init__(self, usertoken, abc, auth_v, ua=""):
        self.s = requests.Session()
        self.s.verify = certifi.where()
        self.s.headers.update({
            "host": "app.vocabgo.com", "usertoken": usertoken, "abc": abc,
            "authorization-v": auth_v, "x-requested-with": "XMLHttpRequest",
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://app.vocabgo.com",
            "referer": "https://app.vocabgo.com/student/",
            "user-agent": ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254173b) XWEB/19027 Flue",
        })

    def _get(self, path, params, base=BASE):
        params = {**params, "timestamp": _ms(), "version": VERSION, "app_type": 1}
        return _decrypt(self.s.get(base + path, params=params, timeout=20).json())

    def _post(self, path, body, base=BASE):
        body = {**body, "timestamp": _ms(), "version": VERSION}
        body["sign"] = _sign(body); body["app_type"] = 1
        return _decrypt(self.s.post(base + path, json=body, timeout=20).json())

    def page_task(self, page=1, size=50, search_type="0"):
        return self._post("/Student/ClassTask/PageTask", {"search_type": search_type, "page_count": page, "page_size": size})
    def task_info(self, task_id, release_id):
        return self._get("/Student/ClassTask/Info", {"task_id": task_id, "release_id": release_id})
    def chose_word_list(self, task_id):
        return self._get("/Student/ClassTask/ChoseWordList", {"task_id": task_id, "task_type": 1})
    def submit_chose_word(self, task_id, word_map):
        return self._post("/Student/ClassTask/SubmitChoseWord", {"task_id": task_id, "word_map": word_map, "chose_err_item": 1, "reset_chose_words": 1})
    def start_answer(self, task_id, release_id):
        return self._get("/Student/ClassTask/StartAnswer", {"task_id": task_id, "task_type": 1, "release_id": release_id, "opt_img_w": 2300, "opt_font_size": 128, "opt_font_c": "#000000", "it_img_w": 2702, "it_font_size": 144})
    def verify(self, topic_code, answer):
        return self._post("/Student/ClassTask/VerifyAnswer", {"topic_code": topic_code, "answer": answer})
    def submit(self, topic_code, time_spent):
        return self._post("/Student/ClassTask/SubmitAnswerAndSave", {"topic_code": topic_code, "time_spent": time_spent, "opt_img_w": 2300, "opt_font_size": 128, "opt_font_c": "#000000", "it_img_w": 2702, "it_font_size": 144})
    def signin(self):
        return self._post("/Student/TaskStudentSignin/Do", {})
    def main_info(self):
        return self._get("/Student/Main", {})

    def study_task_list(self, course_id="CET4_v2"):
        return self._get("/Student/StudyTask/List", {"course_id": course_id}, base=STUDENT_BASE)
    def study_start_task(self, course_id, list_id, task_type=3, grade=2):
        return self._post("/Student/StudyTask/StartTask", {"course_id": course_id, "list_id": list_id, "task_type": task_type, "grade": grade}, base=STUDENT_BASE)
    def study_task_info(self, task_id, course_id, list_id, task_type=3, grade=2):
        return self._get("/Student/StudyTask/Info", {"task_id": task_id, "course_id": course_id, "list_id": list_id, "task_type": task_type, "grade": grade}, base=STUDENT_BASE)
    def study_chose_word_list(self, task_id, course_id, list_id, task_type=3, grade=2):
        return self._get("/Student/StudyTask/ChoseWordList", {"task_id": task_id, "course_id": course_id, "list_id": list_id, "task_type": task_type, "grade": grade}, base=STUDENT_BASE)
    def study_submit_chose_word(self, task_id, course_id, list_id, word_map, task_type=3, grade=2):
        return self._post(
            "/Student/StudyTask/SubmitChoseWord",
            {
                "task_id": task_id,
                "task_type": task_type,
                "grade": grade,
                "course_id": course_id,
                "list_id": list_id,
                "word_map": word_map,
                "chose_err_item": 1,
                "reset_chose_words": 1,
            },
            base=STUDENT_BASE,
        )
    def study_start_answer(self, task_id, course_id, list_id, task_type=3, grade=2):
        return self._get(
            "/Student/StudyTask/StartAnswer",
            {"task_id": task_id, "task_type": task_type, "grade": grade, "course_id": course_id, "list_id": list_id, "opt_img_w": 2300, "opt_font_size": 128, "opt_font_c": "#000000", "it_img_w": 2702, "it_font_size": 144},
            base=STUDENT_BASE,
        )
    def study_verify(self, topic_code, answer):
        return self._post("/Student/StudyTask/VerifyAnswer", {"topic_code": topic_code, "answer": answer}, base=STUDENT_BASE)
    def study_submit(self, topic_code, time_spent):
        return self._post(
            "/Student/StudyTask/SubmitAnswerAndSave",
            {"topic_code": topic_code, "time_spent": time_spent, "opt_img_w": 2300, "opt_font_size": 128, "opt_font_c": "#000000", "it_img_w": 2702, "it_font_size": 144},
            base=STUDENT_BASE,
        )

# ── 主逻辑 ──
def _get_topic(resp):
    d = (resp or {}).get("data") or {}
    if isinstance(d, dict) and d.get("topic_code"): return d
    return d.get("topic_info") or d.get("topic") or (d.get("topic_list") or [None])[0]

def run_quiz(client, task_id, release_id=None, task_kind="class", course_id=None, list_id=None, task_type=3, grade=2):
    bank = _bank_load()
    word_defs = {}
    print(f"📚 题库 {len(bank)} 条")

    if task_kind == "study":
        resp = client.study_start_answer(task_id, course_id, list_id, task_type=task_type, grade=grade)
    else:
        resp = client.start_answer(task_id, release_id)
    topic = _get_topic(resp)
    if not topic:
        print(f"❌ StartAnswer 失败: {resp}"); return

    d = resp.get("data") or {}
    done, total = d.get("topic_done_num", 0), d.get("topic_total", 0)
    print(f"🚀 开始 {done}/{total}")

    while topic and topic.get("topic_code"):
        code = topic["topic_code"]
        mode = topic.get("topic_mode")
        stem_obj = topic.get("stem") or {}
        stem = stem_obj.get("content", "?")
        remark = stem_obj.get("remark", "") or ""
        done_now = topic.get("topic_done_num", done)
        total_now = topic.get("topic_total", total)

        if mode == 0:
            word = stem.strip().lower()
            opts = topic.get("options") or []
            defs = [o.get("content", "") for o in opts if o.get("content")]
            if defs: word_defs[word] = defs
            print(f"  [{done_now}/{total_now}] 📖 {stem} ({len(defs)}个释义)")
            if task_kind == "study":
                save = client.study_submit(code, random.randint(500, 1500))
            else:
                save = client.submit(code, random.randint(500, 1500))
        elif _is_collocation(topic):
            # 搭配题: 多选, 循环 verify 直到 over_status=1
            opts = topic.get("options") or []
            answer_num = topic.get("answer_num") or 2
            key = _topic_key(topic)

            # 取候选答案 tag 列表
            tags = None; src = None
            if key in bank:
                cached = bank[key]["ans"]
                if isinstance(cached, list):
                    tags = list(cached); src = "cache"
            if not tags:
                tags = _match_collocation(remark, opts)
                if tags: src = "match"
            if not tags:
                # 兜底: 让 LLM 选; 然后只取一个, 后面靠服务器纠错补齐
                a_one = _llm_answer(topic, word_defs)
                if isinstance(a_one, int):
                    tags = [a_one]; src = "llm"
            if not tags:
                tags = [0]; src = "guess"

            chosen, save, ok_tags = [], None, []
            cur_code = code
            for ans_tag in tags[:answer_num]:
                if ans_tag in chosen: continue
                if task_kind == "study":
                    vr = client.study_verify(cur_code, ans_tag)
                else:
                    vr = client.verify(cur_code, ans_tag)
                vd = vr.get("data") or {}
                cur_code = vd.get("topic_code", cur_code)
                if vd.get("answer_result") == 1:
                    ok_tags.append(ans_tag)
                chosen.append(ans_tag)
                # 如果服务端给出 corrects, 用它
                cs = vd.get("answer_corrects") or []
                if cs:
                    for c in cs:
                        if c not in chosen and c not in ok_tags:
                            ok_tags.append(c)
                if vd.get("over_status") == 1:
                    break
                _sleep(0.5, 1.0)

            # 缓存正确答案
            if ok_tags:
                bank[key] = {"ans": ok_tags, "stem": stem}
                _bank_save(bank)

            disp = ",".join(_disp_answer(opts, t, mode) for t in (ok_tags or chosen))
            tag = "✅" if ok_tags else "⚠️"
            print(f"  [{done_now}/{total_now}] {tag} 🔗 {stem} → {disp} [{src}]")

            spent = random.randint(2000, 4000)
            if task_kind == "study":
                save = client.study_submit(cur_code, spent)
            else:
                save = client.submit(cur_code, spent)
        else:
            key = _topic_key(topic)
            opts = topic.get("options") or []
            answer = None; src = None

            # 1. 题库缓存
            if key in bank:
                answer = bank[key]["ans"]; src = "cache"

            # 2. 规则匹配
            if answer is None:
                answer = _match_answer(topic, word_defs)
                if answer is not None: src = "match"

            # 3. LLM 兜底
            if answer is None:
                answer = _llm_answer(topic, word_defs)
                if answer is not None: src = "llm"

            # 4. 实在不行盲猜
            if answer is None:
                answer = 0; src = "guess"

            # Verify
            if task_kind == "study":
                vr = client.study_verify(code, answer)
            else:
                vr = client.verify(code, answer)
            vd = vr.get("data") or {}
            code = vd.get("topic_code", code)
            ar = vd.get("answer_result")

            if ar == 1:
                if key not in bank:
                    bank[key] = {"ans": answer, "stem": stem}
                    _bank_save(bank)
                disp = _disp_answer(opts, answer, mode)
                print(f"  [{done_now}/{total_now}] ✅ {_disp_stem(stem, remark)} → {disp} [{src}]")
            else:
                corrects = vd.get("answer_corrects") or []
                if corrects:
                    answer = corrects[0]
                    bank[key] = {"ans": answer, "stem": stem}
                    _bank_save(bank)
                disp = _disp_answer(opts, answer, mode)
                print(f"  [{done_now}/{total_now}] ⚠️ {_disp_stem(stem, remark)} → {disp} [{src}→fix]")

            spent = random.randint(2000, 4000)
            if task_kind == "study":
                save = client.study_submit(code, spent)
            else:
                save = client.submit(code, spent)

        next_t = _get_topic(save)
        sd = save.get("data") or {}
        done = sd.get("topic_done_num", done + 1)
        total = sd.get("topic_total", total)

        if not next_t or not next_t.get("topic_code") or next_t.get("topic_code") == topic["topic_code"]:
            print(f"🎉 全部完成 {done}/{total}, 题库 {len(bank)} 条, 词典 {len(word_defs)} 词")
            return

        topic = next_t
        _sleep(0.3 if mode == 0 else 2.0, 0.6 if mode == 0 else 4.0)

def _disp_stem(stem, remark):
    s = stem[:35]
    if isinstance(remark, list):
        return s
    if remark: s += f" ({remark[:10]})"
    return s

def _disp_answer(opts, answer, mode):
    if mode == 32 and isinstance(answer, str):
        return answer[:30]
    if isinstance(answer, int) and 0 <= answer < len(opts):
        return opts[answer].get("content", "?")[:30]
    return str(answer)[:30]

def run_full(client, task_id=None, release_id=None, task_index=0, max_score=10):
    if task_id is None or release_id is None:
        resp = client.page_task()
        recs = (resp.get("data") or {}).get("records") or []
        if not recs: print("❌ 没有任务"); return
        for i, r in enumerate(recs):
            print(f"  [{i}] {r['task_name']}  进度{r.get('progress')}%  分数{r.get('score')}")
        chosen = recs[task_index]
        task_id, release_id = chosen["task_id"], chosen["release_id"]
        print(f"→ 选中: {chosen['task_name']}")

    info = client.task_info(task_id, release_id)
    print(f"📋 {(info.get('data') or {}).get('task_name', '?')}")
    _sleep(0.5, 1.0)

    chose = client.chose_word_list(task_id)
    words = (chose.get("data") or {}).get("word_list") or []
    todo = [w for w in words if w.get("score", 0) < max_score]
    print(f"📝 总{len(words)}词, 待练{len(todo)}词")

    if todo:
        word_map = {}
        for w in todo:
            key = f"{w['course_id']}:{w['list_id']}"
            word_map.setdefault(key, []).append(w["word"])
        client.submit_chose_word(task_id, word_map)
        _sleep(0.5, 1.0)
    else:
        print("全部满分, 跳过选词")

    run_quiz(client, task_id, release_id)

    _sleep(0.5, 1.0)
    sr = client.signin()
    sd = sr.get("data") or {}
    if sd:
        print(f"🏆 签到完成, 累计{sd.get('sign_in_total')}天, 积分+{sd.get('integral')}")

def run_study_full(client, task_id=None, course_id="CET4_v2", list_id=None, task_type=3, grade=2, task_index=0, max_score=10):
    try:
        grade = int(grade)
    except (TypeError, ValueError):
        grade = 2

    if not list_id:
        resp = client.study_task_list(course_id=course_id)
        recs = (resp.get("data") or {}).get("task_list") or []
        if not recs: print("❌ 没有自学任务"); return
        for i, r in enumerate(recs):
            print(f"  [{i}] {r['task_name']}  进度{r.get('progress')}%  分数{r.get('score')}")
        chosen = recs[task_index]
        task_id = chosen.get("task_id")
        list_id = chosen.get("list_id")
        task_type = chosen.get("task_type") or task_type
        grade = chosen.get("grade") or grade
        course_id = chosen.get("course_id") or course_id
        print(f"→ 选中: {chosen['task_name']}")

    if task_id is None:
        task_id = -1
    try:
        task_id_num = int(task_id)
    except (TypeError, ValueError):
        task_id_num = -1
    if task_id_num <= 0:
        start = client.study_start_task(course_id, list_id, task_type=task_type, grade=grade)
        sd = start.get("data") or {}
        task_id = sd.get("task_id") or sd.get("id") or task_id
        try:
            task_id_num = int(task_id)
        except (TypeError, ValueError):
            task_id_num = -1
        if task_id_num <= 0:
            latest = client.study_task_list(course_id=course_id)
            recs = (latest.get("data") or {}).get("task_list") or []
            for r in recs:
                if str(r.get("list_id")) == str(list_id):
                    task_id = r.get("task_id") or task_id
                    task_type = r.get("task_type") or task_type
                    grade = r.get("grade") or grade
                    course_id = r.get("course_id") or course_id
                    break
        if not sd and start.get("code") not in (None, 1):
            print(f"⚠️ StartTask 返回: {start}")
        print(f"🆕 自学任务已创建/启动: task_id={task_id}")

    info = client.study_task_info(task_id, course_id, list_id, task_type=task_type, grade=grade)
    print(f"📋 {(info.get('data') or {}).get('task_name') or list_id}")
    _sleep(0.5, 1.0)

    chose = client.study_chose_word_list(task_id, course_id, list_id, task_type=task_type, grade=grade)
    words = (chose.get("data") or {}).get("word_list") or []
    if not words:
        print(f"⚠️ 自学选词列表为空/异常，停止进入答题: {chose}")
        return
    todo = [w for w in words if w.get("score", 0) < max_score]
    print(f"📝 总{len(words)}词, 待练{len(todo)}词")

    if todo:
        word_map = {}
        for w in todo:
            key = f"{w.get('course_id') or course_id}:{w.get('list_id') or list_id}"
            word_map.setdefault(key, []).append(w["word"])
        saved = client.study_submit_chose_word(task_id, course_id, list_id, word_map, task_type=task_type, grade=grade)
        if saved.get("code") != 1:
            print(f"⚠️ 选词返回: {saved}")
        _sleep(0.5, 1.0)
    else:
        print("全部满分, 跳过选词")

    run_quiz(client, task_id=task_id, task_kind="study", course_id=course_id, list_id=list_id, task_type=task_type, grade=grade)

    _sleep(0.5, 1.0)
    sr = client.signin()
    sd = sr.get("data") or {}
    if sd:
        print(f"🏆 签到完成, 累计{sd.get('sign_in_total')}天, 积分+{sd.get('integral')}")

def main():
    config = get_runtime_config()
    missing = get_missing_auth_fields(config)
    if missing:
        raise SystemExit(f"缺少必要配置: {', '.join(missing)}。请先在 .env 或 Web 控制台中填写。")

    client = Client(
        usertoken=config["USERTOKEN"],
        abc=config["ABC"],
        auth_v=config["AUTH_V"],
        ua=config.get("USER_AGENT", ""),
    )
    run_full(client)


if __name__ == "__main__":
    main()






