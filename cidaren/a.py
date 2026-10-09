"""
词达人 (vocabgo) 全自动答题脚本 v3
支持 mode=0/11/31/32 全题型, 匹配不上时用 LLM 兜底
"""

import base64, hashlib, json, math, os, random, re, sys, time, requests, uuid, certifi
from contextlib import contextmanager
from pathlib import Path

try:
    from .config import get_missing_auth_fields, get_runtime_config
    from .bank_store import BankError, default_store, prepare_default_store, encode_answer, tag_for_option, norm
    from .task_categories import classify_task
    from .task_safety import SafetyError, SafetyPaused, SafetyStore, account_key
except ImportError:  # pragma: no cover
    from config import get_missing_auth_fields, get_runtime_config
    from bank_store import BankError, default_store, prepare_default_store, encode_answer, tag_for_option, norm
    from task_categories import classify_task
    from task_safety import SafetyError, SafetyPaused, SafetyStore, account_key

SALT = "ajfajfamsnfaflfasakljdlalkflak"
VERSION = "2.7.0.260507_01"
BASE = "https://app.vocabgo.com/studentv1/api"
STUDENT_BASE = "https://app.vocabgo.com/student/api"
WORD_PAT = re.compile(r'\{(\w+)\}')
NO_PENDING_WORDS_MESSAGE = "全部单词已满分，本轮无需开始新练习"

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
def _norm(s): return norm(s)


class TaskError(RuntimeError):
    """A task response cannot advance the current practice safely."""

    def __init__(self, message, *, code=None, action=None):
        super().__init__(message)
        self.code = code
        self.action = action
        self.definitive_response = False


def _task_error(action, response):
    code = response.get("code") if isinstance(response, dict) else None
    message = response.get("msg") if isinstance(response, dict) else None
    message = f"{action}失败（code={code}）：{message or '服务器返回格式异常'}"
    config = get_runtime_config()
    for key in ("USERTOKEN", "ABC", "AUTH_V", "USER_AGENT", "LLM_KEY"):
        secret = str(config.get(key) or "")
        if secret:
            message = message.replace(secret, "[已隐藏凭据]")
    message = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [已隐藏凭据]", message)
    error = TaskError(message[:500], code=code, action=action)
    error.definitive_response = (type(code) is int or isinstance(code, str) and code.isdigit()) and code not in (1, "1")
    return error


def _task_data(response, action, *, allow_empty=False):
    if (not isinstance(response, dict) or isinstance(response.get("code"), bool)
            or response.get("code") not in (1, "1")):
        raise _task_error(action, response)
    data = response.get("data")
    if isinstance(data, dict):
        return data
    if allow_empty and data is None:
        return {}
    raise TaskError(f"{action}失败：服务器 data 不是对象", action=action)


def _returned_task_id(data, previous):
    value = data.get("task_id")
    if value is None:
        return previous
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TaskError("任务状态异常：服务器返回的 task_id 无效")
    try:
        task_id = int(value)
    except (TypeError, ValueError):
        task_id = 0
    if task_id <= 0:
        raise TaskError("任务状态异常：服务器返回的 task_id 无效")
    return task_id


def _word_score(word):
    if not isinstance(word, dict):
        raise TaskError("选词列表异常：单词记录不是对象")
    try:
        score = float(word.get("score") or 0)
    except (TypeError, ValueError):
        raise TaskError("选词列表异常：单词分数不是数字") from None
    if not math.isfinite(score):
        raise TaskError("选词列表异常：单词分数不是有限数字")
    return score


def _selection_map(words, course_id=None, list_id=None):
    word_map = {}
    for word in words:
        course = word.get("course_id") or course_id
        unit = word.get("list_id") or list_id
        text = word.get("word")
        if not course or not unit or not isinstance(text, str) or not text.strip():
            raise TaskError("选词列表异常：缺少课程、词表或单词，未提交选词")
        selected = word_map.setdefault(f"{course}:{unit}", [])
        if text not in selected:
            selected.append(text)
    return word_map


def _chat_completions_url(llm_url: str) -> str:
    base = llm_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"

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
    elif _is_collocation(topic):
        prompt = f"""{wd_str}题目: 选择与「{stem}」匹配的搭配词
搭配提示: {remark}
需选数量: {topic.get('answer_num') or 2}
选项:
{opts_str}

只回答逗号分隔的选项编号(如: 0,2)，不要其他文字。"""
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
            if not opts:
                return ans_text or None
            if _is_collocation(topic):
                parts = [p for p in re.split(r'[,，\s]+', ans_text) if p]
                if parts and all(re.fullmatch(r'\d+', p) for p in parts):
                    indexes = list(dict.fromkeys(int(p) for p in parts))
                    if all(0 <= i < len(opts) for i in indexes):
                        return [tag_for_option(opts[i], i) for i in indexes]
                return None
            m = re.fullmatch(r'\d+', ans_text)
            if m:
                i = int(m.group())
                return tag_for_option(opts[i], i) if i < len(opts) else None
            ans_norm = _norm(ans_text)
            matches = [tag_for_option(opt,i) for i,opt in enumerate(opts) if ans_norm and ans_norm == _norm(opt.get('content'))]
            return matches[0] if len(matches) == 1 else None
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
    if mode in (15, 21, 22):
        word = norm(stem)
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
    for i, opt in enumerate(opts):
        c = (opt.get("content") or "").strip().lower()
        if c in relations:
            if sum(norm(o.get('content')) == norm(opt.get('content')) for o in opts) != 1:
                return None
            tags.append(tag_for_option(opt,i))
    return tags if tags else None

def _match_word_to_def(target, opts, word_defs):
    defs = word_defs.get(target)
    if not defs:
        return None
    defs_norm = {_norm(d) for d in defs}
    matches = [tag_for_option(opt,i) for i,opt in enumerate(opts) if _norm(opt.get('content')) in defs_norm]
    return matches[0] if len(matches) == 1 else None

def _match_mode32(stem, remark, opts, word_defs):
    if not remark: return None
    opt_words = [o.get("content", "") for o in opts]
    n_blanks = stem.count('_')

    # 从 word_defs 里找: 哪个 word 的某个释义包含 remark 关键词?
    # 然后尝试用该 word 的变形 + 常见搭配组成短语
    # 这个比较难自动化, 交给 LLM 处理
    return None

# ── 客户端 ──
def _denied(message):
    return TaskError(message, action="执行资格检查")


def _strict_int(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
        return int(value)
    return None


def _response_code(response):
    return _strict_int(response.get("code")) if isinstance(response, dict) else None


def _valid_code(code):
    return type(code) is int and code > 0 or isinstance(code, str) and bool(code.strip())


def _complete_topic(topic, task_kind):
    if not isinstance(topic, dict) or not _valid_code(topic.get("topic_code")):
        raise SafetyError("服务器未返回完整题目凭据，已停止推进")
    mode = _strict_int(topic.get("topic_mode"))
    stem, options = topic.get("stem"), topic.get("options")
    wanted = 3 if task_kind == "study" else 1
    if "task_type" in topic and _strict_int(topic["task_type"]) != wanted:
        raise SafetyError("题目类型不属于当前普通学习任务")
    if mode is None or mode < 0 or not isinstance(stem, dict) or not isinstance(stem.get("content"), str):
        raise SafetyError("服务器题目缺少有效题型或题干，已停止推进")
    if not stem["content"].strip() and mode not in {21, 22}:
        raise SafetyError("服务器题干为空，不能推断为阅读题")
    if options is None and mode in {51, 52, 53, 54, 61, 62, 71, 72, 73}:
        options = []
    if not isinstance(options, list) or any(not isinstance(option, dict) for option in options):
        raise SafetyError("服务器题目缺少有效选项结构")
    if any(not isinstance(option.get("content"), str) or not option["content"].strip() for option in options):
        raise SafetyError("选项文字不完整，未查询词库或调用模型")
    if mode != 0 and options:
        tags = [option.get("answer_tag") for option in options]
        if any(type(tag) not in {int, str} or isinstance(tag, str) and not tag.strip() for tag in tags) or len({str(tag) for tag in tags}) != len(tags):
            raise SafetyError("选择题缺少唯一真实 answer_tag，未发送答案")
    if mode == 0 and (not options or any(not isinstance(option.get("content"), str) for option in options)):
        raise SafetyError("阅读题缺少完整释义，不能直接保存")
    if mode not in {51, 52, 53, 54, 61, 62, 71, 72, 73} and not options:
        raise SafetyError("选择题没有有效选项，已停止推进")
    for field in ("topic_done_num", "topic_total"):
        if field in topic and (_strict_int(topic[field]) is None or _strict_int(topic[field]) < 0):
            raise SafetyError("服务器题目计数字段无效")
    topic["topic_mode"] = mode
    return topic


def _strict_verification(data):
    if (not isinstance(data, dict) or not _valid_code(data.get("topic_code"))
            or type(data.get("answer_result")) is not int or data["answer_result"] not in {0, 1}
            or type(data.get("over_status")) is not int or data["over_status"] not in {0, 1}):
        raise SafetyError("验证响应缺少明确的题码、答案结果或结束状态")
    if "clean_status" in data and (type(data["clean_status"]) is not int or data["clean_status"] not in {0, 1, 2}):
        raise SafetyError("验证响应中的选择保留状态无效")
    return data


class Client:
    def __init__(self, usertoken, abc, auth_v, ua="", *, expected_account_key=None, safety_store=None):
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
        self.expected_account_key = expected_account_key
        self.safety_store = safety_store or SafetyStore(Path(__file__).resolve().parents[1])
        self._task_context = None
        self._task_metadata = None
        self._task_ids = set()
        self._scope_headers = None
        self._active_topic = None
        self._current_code = None
        self._topic_aliases = []
        self._verification_over = False
        self._saved_topic = False
        self._batch_finished = False

    @staticmethod
    def _read_request(callback):
        for attempt in range(3):
            try:
                return callback()
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise
                print(f"⚠️ 只读请求暂时失败，重试 {attempt + 1}/2")
                time.sleep(0.5 * (attempt + 1))

    def _credentials(self):
        return tuple(self.s.headers.get(key) for key in ("usertoken", "abc", "authorization-v", "user-agent"))

    def get_account_key(self):
        data = _task_data(self.main_info(), "读取账户信息")
        user = data.get("user_info")
        if not isinstance(user, dict):
            raise _denied("无法确认当前账户的稳定编号，禁止运行任务")
        key = account_key(user)
        if self.expected_account_key and key != self.expected_account_key:
            raise _denied("当前账户与启动时绑定的账户不一致，禁止运行旧任务")
        return key

    def _lookup_task(self, task_kind, task_id, release_id, course_id, list_id):
        if task_kind == "class":
            matches = []
            for page in range(1, 101):
                data = _task_data(self.page_task(page=page, size=200), "读取班级任务")
                records = data.get("records")
                if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                    raise _denied("班级任务列表格式异常，不能确认任务归属")
                matches.extend(record for record in records
                               if str(record.get("release_id")) == str(release_id))
                total = data.get("total")
                if len(records) < 200 or type(total) is int and page * 200 >= total:
                    break
            if len(matches) != 1:
                raise _denied("任务不在当前账户的班级列表中，或任务归属不唯一")
            record = dict(matches[0])
            if str(record.get("task_id")) != str(task_id):
                raise _denied("任务编号与当前账户的发布记录不匹配，请刷新列表")
            record["source"] = "class"
        elif task_kind == "study":
            data = _task_data(self.study_task_list(course_id), "读取自学任务")
            records = data.get("task_list")
            if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                raise _denied("自学任务列表格式异常，不能确认任务归属")
            matches = [record for record in records if str(record.get("list_id")) == str(list_id)]
            if len(matches) != 1:
                raise _denied("词表不在当前账户的课程列表中，或词表归属不唯一")
            record = dict(matches[0])
            if record.get("course_id") not in (None, course_id):
                raise _denied("自学词表所属课程不匹配")
            current_id = record.get("task_id")
            if str(current_id) != str(task_id) and str(task_id) not in {"-1", "0", "None"}:
                raise _denied("自学任务编号与课程词表不匹配，请刷新列表")
            record.update(source="study", course_id=course_id, list_id=list_id)
        else:
            raise _denied("当前程序只支持普通学习和自学任务")
        classified = classify_task(record)
        wanted_type = 1 if task_kind == "class" else 3
        if _strict_int(record.get("task_type")) != wanted_type or not classified.get("can_start"):
            raise _denied(classified.get("eligibility_reason") or "任务类型未确认，禁止答题")
        return record

    @contextmanager
    def task_scope(self, task_kind, task_id=None, release_id=None, course_id=None, list_id=None, task_type=None, grade=2):
        expected_type = 1 if task_kind == "class" else 3
        if task_type is not None and _strict_int(task_type) != expected_type:
            raise _denied("调用参数与普通学习任务类型不一致")
        if self._task_context is not None:
            payload = {"task_id": task_id}
            if task_kind == "class":
                payload["release_id"] = release_id
            else:
                payload.update(course_id=course_id, list_id=list_id, task_type=task_type or 3)
            self._guard_request("/Student/" + ("ClassTask" if task_kind == "class" else "StudyTask") + "/StartAnswer", payload)
            yield self._task_context
            return
        key = self.get_account_key()
        metadata = self._lookup_task(task_kind, task_id, release_id, course_id, list_id)
        with self.safety_store.scope(key, task_kind, release_id=release_id, course_id=course_id, list_id=list_id) as context:
            self._task_context = context
            self._task_metadata = metadata
            self._task_ids = {str(metadata.get("task_id")), str(task_id)}
            self._scope_headers = self._credentials()
            self._active_topic = self._current_code = None
            self._topic_aliases = []
            self._verification_over = self._saved_topic = self._batch_finished = False
            try:
                if not context.round_id:
                    context.begin_round()
                yield context
            finally:
                self._task_context = self._task_metadata = self._scope_headers = None
                self._active_topic = self._current_code = None
                self._topic_aliases = []
                self._task_ids = set()

    def _guard_request(self, path, payload):
        context, metadata = self._task_context, self._task_metadata
        if context is None or metadata is None:
            raise _denied("任务推进需要先确认账户、任务类型和归属，禁止直接调用")
        if self._credentials() != self._scope_headers:
            context.pause("运行期间鉴权信息发生变化，已暂停任务")
        source = metadata["source"]
        if "/ClassTask/" in path and source != "class" or "/StudyTask/" in path and source != "study":
            raise _denied("当前执行上下文与请求的任务来源不一致")
        wanted = 1 if source == "class" else 3
        if "task_type" in payload and _strict_int(payload["task_type"]) != wanted:
            raise _denied("请求任务类型与服务端元数据不一致")
        for field in (("release_id",) if source == "class" else ("course_id", "list_id")):
            if field in payload and str(payload[field]) != str(metadata.get(field)):
                raise _denied("请求的发布编号或课程词表与已确认任务不一致")
        if "task_id" in payload and str(payload["task_id"]) not in self._task_ids:
            raise _denied("请求任务编号不属于当前执行上下文")
        if "topic_code" in payload:
            if self._active_topic is None or str(payload["topic_code"]) != str(self._current_code):
                context.pause("题目操作凭据与当前题目不一致，禁止继续提交")
            if self._saved_topic:
                context.pause("当前题目已尝试保存，禁止再次验证或保存")
        if path.endswith("/SubmitAnswerAndSave"):
            if self._active_topic is None or not (self._active_topic["topic_mode"] == 0 or self._verification_over):
                context.pause("服务器尚未明确结束本题验证，禁止保存")
            self._saved_topic = True
        if path.endswith("/Do") and not self._batch_finished:
            raise _denied("本轮尚未收到明确结束响应，禁止签到")

    def pause(self, reason):
        if self._task_context is not None:
            self._task_context.pause(reason)
        raise SafetyPaused(reason)

    def activate_topic(self, topic, *, registered=False):
        if self._task_context is None:
            raise _denied("答题需要任务执行上下文")
        _complete_topic(topic, self._task_metadata["source"])
        if not registered and self._task_context.is_seen(topic["topic_code"]):
            self.pause("服务器返回已经使用过的题目凭据，已暂停以避免重复保存")
        # Receiving a full next question is not a submission. Keep it resumable
        # after an ordinary stop; Verify/Save persist the codes they actually use.
        self._active_topic = topic
        self._current_code = topic["topic_code"]
        self._topic_aliases = [topic["topic_code"]]
        self._verification_over = self._saved_topic = False

    def _validate_advance(self, path, response):
        action = path.rsplit("/", 1)[-1]
        if _response_code(response) == 20001 and action in {"StartAnswer", "SubmitAnswerAndSave"}:
            if ("data" not in response or response["data"] is not None and not isinstance(response["data"], dict)
                    or "msg" in response and not isinstance(response["msg"], str)):
                raise SafetyError("需要选词响应结构不完整，不能确认当前操作结果")
            self._observe_task_metadata(response["data"])
            if action == "SubmitAnswerAndSave":
                self._batch_finished = True
            return
        data = _task_data(response, action, allow_empty=action in {"SubmitChoseWord", "StartTask", "Do"})
        if action == "StartAnswer":
            topic = _complete_topic(_get_topic(response), self._task_metadata["source"])
            if self._task_context.is_seen(topic["topic_code"]):
                self.pause("启动响应返回已经处理过的题目凭据，已暂停")
        elif action == "VerifyAnswer":
            _strict_verification(data)
            code = data["topic_code"]
            if str(code) != str(self._current_code):
                if self._task_context.is_seen(code):
                    self.pause("验证响应回到了已经使用过的题目凭据")
            self._current_code = code
            if not any(str(alias) == str(code) for alias in self._topic_aliases):
                self._topic_aliases.append(code)
            self._verification_over = data["over_status"] == 1
        elif action == "SubmitAnswerAndSave":
            topic = _get_topic(response)
            _complete_topic(topic, self._task_metadata["source"])
            if (any(str(alias) == str(topic["topic_code"]) for alias in self._topic_aliases)
                    or self._task_context.is_seen(topic["topic_code"])):
                self.pause("保存后返回旧题目凭据，无法确认题目推进，已暂停")
        self._observe_task_metadata(data)

    def _observe_task_metadata(self, data):
        if self._task_context is None or not isinstance(data, dict):
            return
        wanted = 1 if self._task_metadata["source"] == "class" else 3
        if "task_type" in data and _strict_int(data["task_type"]) != wanted:
            self.pause("服务器返回的任务类型与当前执行上下文不一致")
        for field in (("release_id",) if self._task_metadata["source"] == "class" else ("course_id", "list_id")):
            if field in data and str(data[field]) != str(self._task_metadata.get(field)):
                self.pause("服务器返回的任务归属与执行上下文不一致")
        if "task_id" in data:
            value = _returned_task_id(data, None)
            self._task_ids.add(str(value))
            self._task_metadata["task_id"] = value

    def _advance(self, path, payload, callback):
        self._guard_request(path, payload)
        if "task_id" in payload and (_strict_int(payload["task_id"]) is None or _strict_int(payload["task_id"]) <= 0):
            raise _denied("任务推进需要已确认的正整数任务编号")
        action = path.rsplit("/", 1)[-1]
        def aliases(response):
            if action == "VerifyAnswer" and _response_code(response) == 1:
                return [payload["topic_code"], response["data"]["topic_code"]]
            if action == "SubmitAnswerAndSave" and _response_code(response) in {1, 20001}:
                return [*self._topic_aliases, payload["topic_code"]]
            return []
        response = self._task_context.execute(action, payload, callback,
                                              lambda value: self._validate_advance(path, value),
                                              confirm_aliases=aliases,
                                              begin_round=lambda value: action in {"SubmitChoseWord", "StartTask"} and _response_code(value) == 1)
        if action in {"StartAnswer", "SubmitAnswerAndSave"} and _response_code(response) == 1:
            self.activate_topic(_get_topic(response), registered=True)
        return response

    def _get(self, path, params, base=BASE):
        params = {**params, "timestamp": _ms(), "version": VERSION, "app_type": 1}
        callback = lambda: _decrypt(self.s.get(base + path, params=params, timeout=20).json())
        if path.endswith("/StartAnswer"):
            return self._advance(path, params, callback)
        allowed = {"/Student/Main", "/Student/ClassTask/Info", "/Student/ClassTask/ChoseWordList",
                   "/Student/StudyTask/List", "/Student/StudyTask/Info", "/Student/StudyTask/ChoseWordList"}
        if path not in allowed:
            raise _denied("未确认只读性质的接口不允许直接请求")
        if self._task_context is not None and path.rsplit("/", 1)[-1] in {"Info", "ChoseWordList"}:
            self._guard_request(path, params)
        response = self._read_request(callback)
        if path.rsplit("/", 1)[-1] in {"Info", "ChoseWordList"} and _response_code(response) == 1:
            self._observe_task_metadata(response.get("data"))
        return response

    def _post(self, path, body, base=BASE):
        body = {**body, "timestamp": _ms(), "version": VERSION}
        body["sign"] = _sign(body); body["app_type"] = 1
        callback = lambda: _decrypt(self.s.post(base + path, json=body, timeout=20).json())
        if path == "/Student/ClassTask/PageTask":
            return self._read_request(callback)
        allowed = {"/Student/ClassTask/SubmitChoseWord", "/Student/ClassTask/VerifyAnswer",
                   "/Student/ClassTask/SubmitAnswerAndSave", "/Student/StudyTask/StartTask",
                   "/Student/StudyTask/SubmitChoseWord", "/Student/StudyTask/VerifyAnswer",
                   "/Student/StudyTask/SubmitAnswerAndSave", "/Student/TaskStudentSignin/Do"}
        if path not in allowed:
            raise _denied("当前程序不允许调用未支持的任务推进接口")
        return self._advance(path, body, callback)

    def page_task(self, page=1, size=50, search_type="0"):
        return self._post("/Student/ClassTask/PageTask", {"search_type": search_type, "page_count": page, "page_size": size})
    def task_info(self, task_id, release_id):
        return self._get("/Student/ClassTask/Info", {"task_id": task_id, "release_id": release_id})
    def chose_word_list(self, task_id):
        return self._get("/Student/ClassTask/ChoseWordList", {"task_id": task_id, "task_type": 1})
    def submit_chose_word(self, task_id, word_map, *, reset=False):
        body = {"task_id": task_id, "word_map": word_map, "chose_err_item": 2}
        if reset:
            body["reset_chose_words"] = 1
        return self._post("/Student/ClassTask/SubmitChoseWord", body)
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
    def study_submit_chose_word(self, task_id, course_id, list_id, word_map, task_type=3, grade=2, *, reset=False):
        body = {
            "task_id": task_id,
            "task_type": task_type,
            "grade": grade,
            "course_id": course_id,
            "list_id": list_id,
            "word_map": word_map,
            "chose_err_item": 2,
        }
        if reset:
            body["reset_chose_words"] = 1
        return self._post("/Student/StudyTask/SubmitChoseWord", body, base=STUDENT_BASE)
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
    d = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(d, dict):
        return None
    if d.get("topic_code"):
        return d
    for key in ("topic_info", "topic"):
        topic = d.get(key)
        if isinstance(topic, dict) and topic.get("topic_code"):
            return topic
    topics = d.get("topic_list")
    if isinstance(topics, list) and topics and isinstance(topics[0], dict):
        return topics[0] if topics[0].get("topic_code") else None
    return None

def _valid_answer(topic, answer):
    if isinstance(answer, list) and not _is_collocation(topic):
        return False
    try:
        encode_answer(topic, answer)
        return True
    except BankError:
        return False


def _remember(bank, topic, answer, source, verification='pending', *, complete=True, detail=None):
    if _valid_answer(topic, answer):
        # Malformed suggestions cannot be learned; database failures propagate.
        return bank.record(topic, answer, source, verification, complete=complete, detail=detail)
    return None


def _select_answer(bank, topic, word_defs):
    hit = bank.lookup(topic)
    if hit.answer is not None:
        return hit.answer, hit.source
    print(f"    [词库未命中] {hit.reason}")
    for source, provider in [('规则', _match_answer), ('LLM', _llm_answer)]:
        if source == 'LLM':
            print('    [LLM回退] 词库未命中，规则未提供可用答案')
        answer = provider(topic, word_defs)
        if _is_collocation(topic) and answer is not None and not isinstance(answer, list):
            answer = [answer]
        if answer is not None and _valid_answer(topic, answer) and not bank.is_rejected(topic, answer):
            return answer, source
    opts = topic.get('options') or []
    answer = tag_for_option(opts[0], 0) if opts else 0
    if _is_collocation(topic):
        answer = [answer]
    return answer, '猜测'


def _verification_data(response):
    return _task_data(response, "验证答案")


def _flag(data, name, value):
    return type(data.get(name)) is int and data[name] == value


def _next_code(data, previous):
    code = data.get('topic_code')
    return code if isinstance(code, (str, int)) and not isinstance(code, bool) and code else previous


def _corrections(topic, data):
    raw = data.get('answer_corrects')
    if not isinstance(raw, (list, str)) or not raw:
        return []
    opts = topic.get('options') or []
    if topic.get('topic_mode') == 32:
        if isinstance(raw, str):
            candidates = [raw]
        elif all(isinstance(x, str) for x in raw):
            candidates = [','.join(raw)]
        elif all(type(x) is int for x in raw):
            words = []
            for tag in raw:
                matches = [o for i,o in enumerate(opts) if str(tag_for_option(o,i)) == str(tag)]
                if len(matches) != 1:
                    return []
                words.append(matches[0].get('content',''))
            candidates = [','.join(words)]
        else:
            return []
    elif opts:
        if not isinstance(raw, list):
            return []
        tags = []
        for item in raw:
            if type(item) not in (str, int):
                return []
            matches = [tag_for_option(o,i) for i,o in enumerate(opts) if str(tag_for_option(o,i)) == str(item)]
            if len(matches) != 1:
                return []
            if matches[0] not in tags:
                tags.append(matches[0])
        candidates = [tags] if _is_collocation(topic) else tags
    else:
        candidates = [raw] if isinstance(raw, str) else raw
        if not all(isinstance(x,str) for x in candidates):
            return []
    return [answer for answer in candidates if _valid_answer(topic, answer)]


def run_quiz(client, task_id, release_id=None, task_kind="class", course_id=None, list_id=None, task_type=3, grade=2, bank=None):
    if not isinstance(client, Client):
        raise _denied("答题执行器需要可核验任务归属的 Client")
    with client.task_scope(task_kind, task_id=task_id, release_id=release_id,
                           course_id=course_id, list_id=list_id,
                           task_type=task_type if task_kind == "study" else None, grade=grade):
        if (task_kind == "study" and (_strict_int(task_id) is None or _strict_int(task_id) <= 0)
                and _strict_int(client._task_metadata.get("task_id")) is not None and _strict_int(client._task_metadata["task_id"]) > 0):
            task_id = _strict_int(client._task_metadata["task_id"])
        bank = bank or default_store()
        with bank.runtime():
            return _run_quiz(client, task_id, release_id, task_kind, course_id, list_id, task_type, grade, bank)


def _verify_question(client, topic, task_kind, bank, word_defs):
    if (not isinstance(client, Client) or client._task_context is None or client._task_metadata is None
            or client._task_metadata.get("source") != task_kind):
        raise _denied("答案学习和验证需要已核验的任务执行上下文")
    _complete_topic(topic, task_kind)
    opts = topic.get("options") or []
    multiple = _is_collocation(topic)
    answer_num = _strict_int(topic.get("answer_num")) if multiple else 1
    if multiple and (answer_num is None or not 1 <= answer_num <= len(opts)):
        client.pause("多选题所需答案数量无效，未发送答案")
    answer, source = _select_answer(bank, topic, word_defs)
    if not _valid_answer(topic, answer):
        client.pause("无法映射有效答案，未发送验证请求")
    _remember(bank, topic, answer, source, complete=not multiple)
    queue = list(answer) if multiple else [answer]
    selected, verified = [], []
    attempts, budget = 0, 2 * max(1, len(opts))
    while queue and attempts < budget:
        chosen = queue.pop(0)
        if multiple and chosen in selected:
            continue
        if multiple and chosen not in [tag_for_option(option, index) for index, option in enumerate(opts)]:
            client.pause("多选答案不能唯一映射到本题选项")
        attempts += 1
        response = client.study_verify(client._current_code, chosen) if task_kind == "study" else client.verify(client._current_code, chosen)
        data = _strict_verification(_verification_data(response))
        if data["answer_result"] == 1:
            if chosen not in verified:
                verified.append(chosen)
        elif _valid_answer(topic, answer):
            bank.reject(topic, answer, detail={"failed_tag": chosen} if multiple else None)
        selected.append(chosen)
        corrections = _corrections(topic, data)
        for corrected in corrections:
            _remember(bank, topic, corrected, "服务器纠错", complete=False if multiple else True)
        if data["over_status"] == 1:
            if multiple:
                if len(verified) != answer_num:
                    client.pause("多选题虽已结束，但答案集合未完整验证，未保存本题")
                _remember(bank, topic, verified, source, "confirmed", complete=True,
                          detail={"fully_verified": True, "verified_tags": verified})
                display = ",".join(_disp_answer(opts, tag, topic["topic_mode"]) for tag in verified)
            else:
                if data["answer_result"] == 1:
                    _remember(bank, topic, chosen, source, "confirmed")
                display = _disp_answer(opts, chosen, topic["topic_mode"])
            return data["answer_result"] == 1, display, source
        # clean_status=2 preserves the current selection. Other values reset it.
        if multiple:
            corrected_tags = [tag for corrected in corrections for tag in corrected]
            if data.get("clean_status") == 2:
                queue.extend(tag for tag in corrected_tags if tag not in selected and tag not in queue)
            else:
                selected, verified = [], []
                queue = list(dict.fromkeys(corrected_tags))
        else:
            queue = [candidate for candidate in corrections if _valid_answer(topic, candidate)]
        if not queue:
            client.pause("服务器尚未结束本题，且没有可验证的新答案，未保存")
        _sleep(0.5, 1.0)
    client.pause("本题验证次数已达到有限重试上限，未保存")


def _run_quiz(client, task_id, release_id, task_kind, course_id, list_id, task_type, grade, bank):
    if client._task_context is None:
        raise _denied("答题必须在已核验的任务上下文中运行")
    word_defs = {}
    counts = bank.status()
    print(f"📚 精确题库 {counts['formal']} 条，临时缓存 {counts['cache']} 条，历史原文 {counts['legacy']} 条")
    if task_kind == "study":
        response = client.study_start_answer(task_id, course_id, list_id, task_type=task_type, grade=grade)
    else:
        response = client.start_answer(task_id, release_id)
    if _response_code(response) == 20001:
        raise _task_error("开始答题", response)
    data = _task_data(response, "开始答题")
    topic = _complete_topic(_get_topic(response), task_kind)
    done, total = data.get("topic_done_num", "?"), data.get("topic_total", "?")
    print(f"🚀 开始 {done}/{total}")
    while True:
        _complete_topic(topic, task_kind)
        if client._active_topic is not topic:
            client.activate_topic(topic)
        mode = topic["topic_mode"]
        stem_obj = topic["stem"]
        stem, remark = stem_obj.get("content", ""), stem_obj.get("remark", "") or ""
        done_now, total_now = topic.get("topic_done_num", done), topic.get("topic_total", total)
        if mode == 0:
            definitions = [option["content"] for option in topic["options"] if option["content"]]
            word = norm(stem)
            if definitions:
                word_defs[word] = list(dict.fromkeys([*word_defs.get(word, []), *definitions]))
                bank.record_definitions(topic)
            print(f"  [{done_now}/{total_now}] 📖 {stem} ({len(definitions)}个释义)")
            spent = random.randint(500, 1500)
        else:
            correct, display, source = _verify_question(client, topic, task_kind, bank, word_defs)
            tag = "✅" if correct else "⚠️"
            print(f"  [{done_now}/{total_now}] {tag} {_disp_stem(stem, remark)} → {display} [{source}]")
            spent = random.randint(2000, 4000)
        saved = client.study_submit(client._current_code, spent) if task_kind == "study" else client.submit(client._current_code, spent)
        if _response_code(saved) == 20001:
            counts = bank.status()
            print(f"🎉 本组选词练习已结束，精确题库 {counts['formal']} 条，临时缓存 {counts['cache']} 条；刷新任务状态后可继续选择")
            return
        saved_data = _task_data(saved, "提交答题")
        next_topic = _complete_topic(_get_topic(saved), task_kind)
        done = saved_data.get("topic_done_num", next_topic.get("topic_done_num", "?"))
        total = saved_data.get("topic_total", next_topic.get("topic_total", "?"))
        topic = next_topic
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
    matches = [opt for i,opt in enumerate(opts) if str(tag_for_option(opt,i)) == str(answer)]
    if len(matches) == 1:
        return matches[0].get('content','?')[:30]
    return str(answer)[:30]

def _run_with_selection(client, task_id, release_id=None, *, task_kind="class", course_id=None,
                        list_id=None, task_type=3, grade=2, max_score=10, bank=None):
    # A stale continue state may fall back to ordinary selection once, never reset repeatedly.
    for attempt in range(2):
        if task_kind == "study":
            response = client.study_chose_word_list(task_id, course_id, list_id, task_type=task_type, grade=grade)
        else:
            response = client.chose_word_list(task_id)
        data = _task_data(response, "读取选词列表")
        task_id = _returned_task_id(data, task_id)
        continuing = attempt == 0 and str(data.get("exist_little_task")) == "1"
        words = data.get("word_list")
        if words is None and continuing:
            words = []
        if not isinstance(words, list):
            raise TaskError("选词列表异常：word_list 不是列表")
        todo = [word for word in words if _word_score(word) < max_score]
        print(f"📝 总{len(words)}词, 待练{len(todo)}词")

        if continuing:
            print("▶ 继续已有练习，不重新选词")
        else:
            if not words:
                raise TaskError("选词列表为空，无法开始练习")
            if not todo:
                print(NO_PENDING_WORDS_MESSAGE)
                return False
            word_map = _selection_map(todo, course_id or data.get("course_id"), list_id or data.get("list_id"))
            if task_kind == "study":
                saved = client.study_submit_chose_word(task_id, course_id, list_id, word_map,
                                                      task_type=task_type, grade=grade)
            else:
                saved = client.submit_chose_word(task_id, word_map)
            saved_data = _task_data(saved, "提交选词", allow_empty=True)
            task_id = _returned_task_id(saved_data, task_id)
            print(f"✅ 选词成功：{len(todo)}词，普通练习")
            _sleep(0.5, 1.0)

        try:
            run_quiz(client, task_id=task_id, release_id=release_id, task_kind=task_kind,
                     course_id=course_id, list_id=list_id, task_type=task_type, grade=grade, bank=bank)
            return True
        except TaskError as exc:
            if continuing and exc.action == "开始答题" and exc.code in (20001, "20001"):
                print("⚠️ 现有练习已转为需要选词，重新读取列表并尝试一次普通选词")
                continue
            raise
    raise TaskError("开始答题失败：选词状态反复变化")


def run_full(client, task_id=None, release_id=None, task_index=0, max_score=10, bank=None):
    if task_id is None or release_id is None:
        resp = client.page_task()
        recs = _task_data(resp, "读取班级任务").get("records") or []
        if not recs: print("❌ 没有任务"); return
        for i, r in enumerate(recs):
            print(f"  [{i}] {r['task_name']}  进度{r.get('progress')}%  分数{r.get('score')}")
        chosen = recs[task_index]
        task_id, release_id = chosen["task_id"], chosen["release_id"]
        print(f"→ 选中: {chosen['task_name']}")

    if not isinstance(client, Client):
        raise _denied("任务执行需要可核验归属的 Client")
    with client.task_scope("class", task_id=task_id, release_id=release_id):
        return _run_class_scoped(client, task_id, release_id, max_score, bank)


def _run_class_scoped(client, task_id, release_id, max_score, bank):
    info = _task_data(client.task_info(task_id, release_id), "读取班级任务信息")
    task_id = _returned_task_id(info, task_id)
    print(f"📋 {info.get('task_name', '?')}")
    _sleep(0.5, 1.0)

    if not _run_with_selection(client, task_id, release_id, max_score=max_score, bank=bank):
        return

    _sleep(0.5, 1.0)
    sr = client.signin()
    sd = sr.get("data") or {}
    if sd:
        print(f"🏆 签到完成, 累计{sd.get('sign_in_total')}天, 积分+{sd.get('integral')}")

def run_study_full(client, task_id=None, course_id="CET4_v2", list_id=None, task_type=3, grade=2, task_index=0, max_score=10, bank=None):
    try:
        grade = int(grade)
    except (TypeError, ValueError):
        grade = 2

    if not list_id:
        resp = client.study_task_list(course_id=course_id)
        recs = _task_data(resp, "读取自学任务").get("task_list") or []
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

    if not isinstance(client, Client):
        raise _denied("任务执行需要可核验归属的 Client")
    with client.task_scope("study", task_id=task_id, course_id=course_id, list_id=list_id,
                           task_type=task_type, grade=grade):
        existing_id = _strict_int(client._task_metadata.get("task_id"))
        if (_strict_int(task_id) is None or _strict_int(task_id) <= 0) and existing_id is not None and existing_id > 0:
            task_id = existing_id
        return _run_study_scoped(client, task_id, course_id, list_id, task_type, grade, max_score, bank)


def _run_study_scoped(client, task_id, course_id, list_id, task_type, grade, max_score, bank):
    if task_id is None:
        task_id = -1
    try:
        task_id_num = int(task_id)
    except (TypeError, ValueError):
        task_id_num = -1
    if task_id_num <= 0:
        start = client.study_start_task(course_id, list_id, task_type=task_type, grade=grade)
        sd = _task_data(start, "创建自学任务", allow_empty=True)
        task_id = sd.get("task_id") or sd.get("id") or task_id
        try:
            task_id_num = int(task_id)
        except (TypeError, ValueError):
            task_id_num = -1
        if task_id_num <= 0:
            latest = client.study_task_list(course_id=course_id)
            recs = _task_data(latest, "读取自学任务").get("task_list") or []
            for r in recs:
                if str(r.get("list_id")) == str(list_id):
                    task_id = r.get("task_id") or task_id
                    task_type = r.get("task_type") or task_type
                    grade = r.get("grade") or grade
                    course_id = r.get("course_id") or course_id
                    break
        try:
            task_id_num = int(task_id)
        except (TypeError, ValueError):
            task_id_num = -1
        if task_id_num <= 0:
            raise TaskError("创建自学任务失败：服务器未返回有效任务编号")
        task_id = _returned_task_id({"task_id": task_id}, task_id)
        client._task_ids.add(str(task_id))
        print(f"🆕 自学任务已创建/启动: task_id={task_id}")

    info = _task_data(client.study_task_info(task_id, course_id, list_id, task_type=task_type, grade=grade),
                      "读取自学任务信息")
    task_id = _returned_task_id(info, task_id)
    print(f"📋 {info.get('task_name') or list_id}")
    _sleep(0.5, 1.0)

    if not _run_with_selection(client, task_id, task_kind="study", course_id=course_id, list_id=list_id,
                               task_type=task_type, grade=grade, max_score=max_score, bank=bank):
        return

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
    records = _task_data(client.page_task(), "读取班级任务").get("records")
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        raise TaskError("没有可选择的班级任务")
    task_id, release_id = records[0].get("task_id"), records[0].get("release_id")
    with client.task_scope("class", task_id=task_id, release_id=release_id):
        bank = prepare_default_store()
        with bank.runtime():
            run_full(client, task_id=task_id, release_id=release_id, bank=bank)


if __name__ == "__main__":
    try:
        main()
    except SafetyError as exc:
        print(f"⏸ 任务已暂停: {exc}", file=sys.stderr)
        raise SystemExit(3)
    except TaskError as exc:
        print(f"❌ 任务状态错误: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except BankError as exc:
        raise SystemExit(f"词库错误: {exc}")
