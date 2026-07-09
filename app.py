import os
import re
import json
import math
import uuid
import base64
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
import anthropic
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify
)

# ═══════════════════════════════════════════════════════════════
# APP SETUP
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me-in-production")

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

# GitHub sync — commits the finished session (answers + generated context
# prompt) to a repo once the user completes all 15 questions.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")          # "owner/repo"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_SESSIONS_PATH = os.environ.get("GITHUB_SESSIONS_PATH", "sessions").strip("/")
GITHUB_API_BASE = "https://api.github.com"

# ═══════════════════════════════════════════════════════════════
# QUESTIONS
# ═══════════════════════════════════════════════════════════════
QUESTIONS = [
    {"id":1,  "theme":"Origin",       "q":"Think of the moment you felt most fully yourself — not performing, not trying. Where were you, and what were you doing?",                               "why":"Reveals your natural state and environment."},
    {"id":2,  "theme":"Wound",        "q":"What is the hardest thing you've been through, and what did it permanently change about how you see the world?",                                     "why":"Core values often calcify around our deepest fractures."},
    {"id":3,  "theme":"Conviction",   "q":"What do you believe to be true about human beings that most people around you would disagree with?",                                                "why":"Contrarian beliefs reveal what you actually think vs. what you have absorbed."},
    {"id":4,  "theme":"Shame",        "q":"What is the version of yourself you are most ashamed of, and what does that shame tell you about what you actually value?",                         "why":"Shame maps the gap between who you are and who you want to be — that gap is values."},
    {"id":5,  "theme":"Obsession",    "q":"What problem or question have you thought about for years without resolution — not for work, just because it will not let you go?",                 "why":"Persistent obsessions are the most honest signal of intellectual identity."},
    {"id":6,  "theme":"Relationship", "q":"Describe the person you have loved or admired most. What did they have that you feel you are still reaching for?",                                  "why":"What we admire in others is what we most deeply want to become."},
    {"id":7,  "theme":"Rage",         "q":"What makes you genuinely angry — not irritated, but morally outraged? Why does that particular thing hit so hard?",                                "why":"Moral anger is a direct read of your value system."},
    {"id":8,  "theme":"Courage",      "q":"What is the thing you know you should do — or say, or become — that you keep finding reasons to postpone?",                                        "why":"Avoidance patterns reveal the values we have not yet committed to living."},
    {"id":9,  "theme":"Legacy",       "q":"If someone described your life at your funeral with full honesty — not flattery — what would you want them to say?",                               "why":"The eulogy question cuts through daily noise to terminal values."},
    {"id":10, "theme":"Pleasure",     "q":"What do you do when no one is watching, nothing is at stake, and you have complete freedom? What does that reveal?",                               "why":"Unobserved behavior is the most honest data point about who you are."},
    {"id":11, "theme":"Betrayal",     "q":"When have you compromised on something that mattered to you? What did you sacrifice, and was it worth it?",                                        "why":"Our compromises reveal the exact hierarchy of our values under pressure."},
    {"id":12, "theme":"Time",         "q":"If you knew you had 5 years left to live — healthy, resourced, free — what would you stop doing immediately? What would you start?",              "why":"Mortality constraints collapse ambiguity about what actually matters."},
    {"id":13, "theme":"Mind",         "q":"What kind of thinking makes you lose track of time? Describe the texture of your best mental state.",                                              "why":"Flow states are the body's signal that we are doing what we were built for."},
    {"id":14, "theme":"Fear",         "q":"What is the story you most fear being true about yourself? Where does that fear come from?",                                                       "why":"Our deepest fears often invert into our deepest values."},
    {"id":15, "theme":"Declaration",  "q":"If you had to distill everything you believe about how a person should live into three sentences — no caveats, no hedging — what would you say?", "why":"Forces the synthesis of everything above into owned conviction."},
]

# ═══════════════════════════════════════════════════════════════
# SESSION PERSISTENCE (disk-backed, keyed by session_id)
# ═══════════════════════════════════════════════════════════════
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "soul_prompt_sessions"))
SESSIONS_DIR.mkdir(exist_ok=True)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _session_filename(name):
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower().strip())[:20].strip("_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:6]
    return f"{slug}_{ts}_{uid}"


class SessionStore:
    """Persists every user action to an individual JSON file in SESSIONS_DIR."""

    def __init__(self, session_id=None):
        self.path = (SESSIONS_DIR / f"{session_id}.json") if session_id else None

    def init(self, name):
        session_id = _session_filename(name)
        self.path = SESSIONS_DIR / f"{session_id}.json"
        self._write({
            "session_id": session_id, "name": name, "started_at": _now(),
            "last_updated": _now(), "answers": {}, "context_prompt": "",
            "top_values": [], "context_generated_at": None, "chat_history": []
        })
        return session_id

    def save_answer(self, idx, theme, question, answer):
        if not self.path:
            return
        d = self._read()
        d["answers"][str(idx)] = {
            "theme": theme, "question": question,
            "answer": answer, "saved_at": _now()
        }
        d["last_updated"] = _now()
        self._write(d)

    def save_context(self, prompt, top_values):
        if not self.path:
            return
        d = self._read()
        d["context_prompt"] = prompt
        d["top_values"] = top_values
        d["context_generated_at"] = _now()
        d["last_updated"] = _now()
        self._write(d)

    def save_chat_turn(self, role, content):
        if not self.path:
            return
        d = self._read()
        d["chat_history"].append({"role": role, "content": content, "timestamp": _now()})
        d["last_updated"] = _now()
        self._write(d)

    def read(self):
        if not self.path or not self.path.exists():
            return None
        return self._read()

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
# GITHUB SYNC
# ═══════════════════════════════════════════════════════════════
def github_configured():
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def push_session_to_github(session_id, data):
    """
    Commit the full session JSON (name, all 15 answers, generated context
    prompt, top values) to GITHUB_REPO at
    {GITHUB_SESSIONS_PATH}/{session_id}.json on GITHUB_BRANCH.

    Returns a dict describing the outcome:
      {"ok": True, "url": "<html url of the file>"}
      {"ok": False, "error": "<message>"}
    """
    if not github_configured():
        return {"ok": False, "error": "GitHub sync not configured (missing GITHUB_TOKEN/GITHUB_REPO)."}

    file_path = f"{GITHUB_SESSIONS_PATH}/{session_id}.json"
    api_url = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/contents/{file_path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    content_str = json.dumps(data, indent=2, ensure_ascii=False)
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")

    try:
        # If the file already exists on this branch, GitHub requires its
        # current sha to update it (shouldn't normally happen for a fresh
        # session id, but handles retries / re-generation safely).
        sha = None
        get_resp = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=10)
        if get_resp.status_code == 200:
            sha = get_resp.json().get("sha")

        payload = {
            "message": f"Add soul-prompt session: {data.get('name', 'unknown')} ({session_id})",
            "content": content_b64,
            "branch": GITHUB_BRANCH,
        }
        if sha:
            payload["sha"] = sha

        put_resp = requests.put(api_url, headers=headers, json=payload, timeout=15)
        if put_resp.status_code in (200, 201):
            html_url = put_resp.json().get("content", {}).get("html_url", "")
            return {"ok": True, "url": html_url}

        try:
            msg = put_resp.json().get("message", put_resp.text)
        except Exception:
            msg = put_resp.text
        return {"ok": False, "error": f"GitHub API {put_resp.status_code}: {msg}"}

    except requests.RequestException as e:
        return {"ok": False, "error": f"Network error contacting GitHub: {e}"}


# ═══════════════════════════════════════════════════════════════
# VALUE SCORING ENGINE (no API — pure algorithm)
# ═══════════════════════════════════════════════════════════════
VALUE_LEXICON = {
    "Integrity":  {"label":"Integrity",  "keywords":["honest","honesty","truth","truthful","real","authentic","genuine","integrity","transparent","principle","principled","trust","betray","betrayed","compromise","mask","perform","fake","lie","values","stand","backbone","conscience","moral","sincere","open","direct","mean what"],"agent_tone":"Be unflinchingly direct. Never soften truth to comfort. This person reads through diplomatic vagueness instantly.","portrait_fragment":"operates from an uncompromising commitment to truth"},
    "Depth":      {"label":"Depth",      "keywords":["meaning","understand","beneath","root","why","question","think","complex","nuance","surface","explore","curious","obsess","read","learn","know","philosophy","idea","theory","mind","intellect","wonder","ponder","study","insight","pattern","structure","framework","concept","abstraction"],"agent_tone":"Go beneath the surface of every topic. Assume sophistication. Skip definitions they already know.","portrait_fragment":"thinks in systems and depths rather than headlines"},
    "Autonomy":   {"label":"Autonomy",   "keywords":["free","freedom","independent","independence","own","control","choose","choice","decide","decision","space","alone","myself","rules","authority","conform","different","unconventional","path","direction","self","sovereign","refuse","resist"],"agent_tone":"Never prescribe. Offer options and reasoning, then let them choose. They reject being told what to do.","portrait_fragment":"moves through life on self-determined terms"},
    "Resilience": {"label":"Resilience", "keywords":["hard","difficult","survive","overcome","failure","loss","broken","recover","strong","persist","through","suffer","pain","wound","scar","rebuild","endure","fight","back","forward","crisis","collapse","fall","rise","again","adversity","setback","bounce"],"agent_tone":"Do not coddle. They have been through real things. Match their toughness.","portrait_fragment":"has been forged by difficulty and carries that hardness as a resource"},
    "Creation":   {"label":"Creation",   "keywords":["build","make","create","design","write","art","music","craft","express","vision","imagine","new","original","invent","produce","project","shape","form","compose","draw","paint","code","develop","launch","ship"],"agent_tone":"Engage with their creative process as a collaborator, not a critic. Ask what they are building.","portrait_fragment":"is fundamentally a maker — someone who thinks by building"},
    "Connection": {"label":"Connection", "keywords":["love","people","relation","friend","family","together","community","belong","share","care","listen","other","bond","trust","human","feel","understand","close","support","give","presence","intimate","attachment","warmth","generous"],"agent_tone":"Remember that behind every question is a human with relationships at stake. Honour that weight.","portrait_fragment":"measures life by the quality of bonds formed"},
    "Excellence": {"label":"Excellence", "keywords":["best","standard","quality","craft","detail","perfect","rigor","discipline","work","improve","master","mastery","skill","precise","sharp","serious","commit","focus","result","execute","high","bar","demand","expect","mediocre","mediocrity","settle","refuse to settle"],"agent_tone":"Hold a high bar in your outputs. They notice lazy thinking and generic answers immediately.","portrait_fragment":"holds themselves and the world around them to an exacting standard"},
    "Courage":    {"label":"Courage",    "keywords":["fear","brave","bravery","risk","dare","bold","vulnerable","afraid","despite","face","stand","fight","uncomfortable","challenge","step","leap","speak up","honest even","difficult"],"agent_tone":"Say the hard thing. They value the uncomfortable truth over reassurance.","portrait_fragment":"chooses discomfort over dishonesty"},
    "Justice":    {"label":"Justice",    "keywords":["fair","fairness","right","wrong","anger","injustice","equal","equality","power","abuse","system","corrupt","broken","outrage","deserve","protect","voice","speak","change","society","moral","responsibility","accountability","inequality","fight for"],"agent_tone":"Do not both-sides every issue. They have strong moral convictions and respect a reasoned position.","portrait_fragment":"carries a sharp moral compass that orients every decision"},
    "Legacy":     {"label":"Legacy",     "keywords":["remember","matter","impact","leave","future","contribution","meaning","purpose","story","life","death","after","mark","world","change","time","build","name","last","outlast","generations","trace","footprint"],"agent_tone":"Help them think long. Connect today's decisions to the arc they are building over a lifetime.","portrait_fragment":"thinks in decades and is quietly building something that should outlast them"},
    "Solitude":   {"label":"Solitude",   "keywords":["alone","quiet","silence","still","space","retreat","inner","reflect","think","peace","withdraw","restore","private","solitary","nature","walk","read","observe","internal","recharge","introvert","stillness"],"agent_tone":"Respect their need for clarity before action. They do not think out loud — they think in private first.","portrait_fragment":"draws energy from stillness and thinks most clearly in solitude"},
    "Growth":     {"label":"Growth",     "keywords":["grow","growth","learn","evolve","change","better","improve","develop","push","stretch","challenge","new","understand","expand","progress","forward","potential","become","transform","next","further","becoming"],"agent_tone":"Frame challenges as developmental, not threatening. They see every difficulty as data.","portrait_fragment":"treats life as a continuous process of becoming"},
}

THEME_AMPLIFIERS = {
    "Origin":       ["Autonomy","Solitude","Creation","Connection"],
    "Wound":        ["Resilience","Integrity","Justice","Courage"],
    "Conviction":   ["Integrity","Justice","Depth","Courage"],
    "Shame":        ["Integrity","Excellence","Courage","Growth"],
    "Obsession":    ["Depth","Creation","Excellence","Growth"],
    "Relationship": ["Connection","Legacy","Excellence","Courage"],
    "Rage":         ["Justice","Integrity","Courage","Connection"],
    "Courage":      ["Courage","Autonomy","Integrity","Growth"],
    "Legacy":       ["Legacy","Connection","Creation","Excellence"],
    "Pleasure":     ["Solitude","Creation","Connection","Autonomy"],
    "Betrayal":     ["Integrity","Resilience","Justice","Courage"],
    "Time":         ["Legacy","Autonomy","Creation","Connection"],
    "Mind":         ["Depth","Creation","Solitude","Excellence"],
    "Fear":         ["Courage","Integrity","Resilience","Growth"],
    "Declaration":  ["Integrity","Legacy","Justice","Depth"],
}

UNIVERSAL_GUIDELINES = [
    "Lead with the answer, then the reasoning. Never bury the insight.",
    "Match their language register — if they are raw, be raw; if they are precise, be precise.",
    "Never use filler phrases like 'Great question!' or 'Certainly!' — they signal inauthenticity.",
    "When you do not know something, say so plainly. Label speculation clearly.",
    "Compress. They will read everything once and expect it to hold weight.",
]


def score_answers(answers):
    scores = defaultdict(float)
    evidence = defaultdict(list)
    for i, q in enumerate(QUESTIONS):
        raw = answers.get(i, "").strip()
        if not raw:
            continue
        text = raw.lower()
        theme = q["theme"]
        words = re.findall(r"[a-z']+", text)
        if not words:
            continue
        amplified = THEME_AMPLIFIERS.get(theme, [])
        for value, data in VALUE_LEXICON.items():
            hits = sum(1 for w in words if w in data["keywords"])
            if hits == 0:
                continue
            amp = 1.45 if value in amplified else 1.0
            tf_score = hits / math.log(len(words) + 2)
            scores[value] += tf_score * amp
            for sent in re.split(r'(?<=[.!?])\s+', raw.strip()):
                if any(kw in sent.lower() for kw in data["keywords"]):
                    frag = sent.strip().rstrip(".!?,;")
                    if 12 < len(frag) < 200 and frag not in [e[0] for e in evidence[value]]:
                        evidence[value].append((frag, theme))
                        break
    return scores, evidence


def infer_cognitive_style(top_values):
    styles, s = [], set(top_values)
    if "Depth" in s and "Solitude" in s:
        styles.append("a deep introvert who processes the world internally before externalizing any conclusion")
    elif "Depth" in s and "Connection" in s:
        styles.append("someone who uses conversation as a thinking tool — they reach clarity through dialogue")
    if "Creation" in s and "Excellence" in s:
        styles.append("a craftsperson who does not separate the idea from its execution")
    if "Justice" in s and "Courage" in s:
        styles.append("someone who will say what others will not, especially when something matters morally")
    if "Legacy" in s and "Resilience" in s:
        styles.append("a long-game thinker who has learned that difficulty is the price of meaning")
    if "Autonomy" in s and "Integrity" in s:
        styles.append("someone who would rather be alone in being right than comfortable in being wrong")
    if "Growth" in s and "Depth" in s:
        styles.append("permanently dissatisfied with where they are — not from anxiety, but from genuine hunger")
    if not styles:
        styles.append("a person who thinks carefully before speaking and means what they say")
    return styles


def infer_friction_points(top_values):
    frictions, s = [], set(top_values)
    if "Integrity" in s:
        frictions.append("vagueness, diplomatic evasion, or answers that hedge everything")
    if "Excellence" in s:
        frictions.append("mediocre thinking, generic advice, or outputs that clearly required no effort")
    if "Autonomy" in s:
        frictions.append("being told what to do, or responses that assume they need to be managed")
    if "Depth" in s:
        frictions.append("shallow takes or oversimplification written for a general audience")
    if "Justice" in s:
        frictions.append("false balance on issues they have already thought through and reached a position on")
    return frictions[:3] if frictions else ["inauthenticity and wasted words"]


def assemble_context_prompt(name, scores, evidence, answers):
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top5 = [v for v, _ in ranked[:5]]
    if not top5:
        top5 = list(VALUE_LEXICON.keys())[:5]
    top5_data = [(v, VALUE_LEXICON[v]) for v in top5]
    cog = infer_cognitive_style(top5)
    frictions = infer_friction_points(top5)
    declaration = answers.get(14, "").strip()

    p1 = VALUE_LEXICON[top5[0]]["portrait_fragment"]
    p2 = VALUE_LEXICON[top5[1]]["portrait_fragment"] if len(top5) > 1 else ""
    portrait = f"You are working with {name} — someone who {p1}"
    if p2:
        portrait += f" and {p2}"
    portrait += f". They are {cog[0]}."
    if len(cog) > 1:
        portrait += f" They are also {cog[1]}."

    vals_block = "CORE VALUES\n" + "─" * 44 + "\n"
    for i, (value, data) in enumerate(top5_data, 1):
        evid_list = evidence.get(value, [])
        evid_line = ""
        if evid_list:
            frag, theme = evid_list[0]
            evid_line = f'\n   [{theme}] "{frag}"'
        vals_block += f"\n{i}. {data['label'].upper()}{evid_line}\n"

    style_block = "HOW THEY THINK\n" + "─" * 44 + "\n"
    for s in cog:
        style_block += f"• They are {s}.\n"

    friction_block = "WHAT THEY CANNOT TOLERATE\n" + "─" * 44 + "\n"
    for f in frictions:
        friction_block += f"• {f.capitalize()}.\n"

    rules_block = "HOW TO BEHAVE AS THEIR AGENT\n" + "─" * 44 + "\n"
    seen = set()
    for value in top5:
        tone = VALUE_LEXICON[value]["agent_tone"]
        if tone not in seen:
            rules_block += f"• {tone}\n"
            seen.add(tone)
    rules_block += "\nUniversal rules:\n"
    for g in UNIVERSAL_GUIDELINES:
        rules_block += f"• {g}\n"

    decl_block = ""
    if declaration:
        decl_block = (
            "\nTHEIR DECLARATION\n" + "─" * 44 + "\n"
            f'In their own words, this is how {name} believes a person should live:\n\n'
            f'"{declaration}"\n\n'
            f"Let this anchor every interaction. When in doubt about what they need, return to this."
        )

    closing = (
        "\nYOUR ROLE\n" + "─" * 44 + "\n"
        f"Be the thinking partner {name} has always wanted but rarely found. "
        "Someone who keeps up, pushes back with substance, respects their autonomy, "
        "and never wastes their time. Your job is not to make them feel good — "
        "it is to help them think more clearly, build more precisely, and move "
        "in the direction that is most true to who they are."
    )

    prompt = "\n".join(["=" * 50, f"AGENT CONTEXT PROMPT — {name.upper()}", "=" * 50, "",
                        portrait, "", vals_block, style_block, "", friction_block, "",
                        rules_block, decl_block, closing])
    return prompt, top5


# ═══════════════════════════════════════════════════════════════
# IN-MEMORY RUNTIME STATE (rebuilt from disk if the process restarts)
# ═══════════════════════════════════════════════════════════════
RUNTIME = {}   # sid -> state dict
STORES = {}    # sid -> SessionStore


def new_session(name):
    store = SessionStore()
    sid = store.init(name)
    STORES[sid] = store
    RUNTIME[sid] = {
        "name": name,
        "answers": {},          # idx -> raw text answer
        "context_prompt": "",
        "top_values": [],
        "chat_history": [],
        "github_status": None,  # set once the questionnaire is completed
    }
    return sid


def get_state(sid):
    if not sid:
        return None, None
    if sid in RUNTIME:
        return RUNTIME[sid], STORES[sid]

    # Try to rebuild from disk (e.g. after a server restart)
    path = SESSIONS_DIR / f"{sid}.json"
    if not path.exists():
        return None, None

    store = SessionStore(sid)
    d = store.read()
    if d is None:
        return None, None

    answers = {int(k): v.get("answer", "") for k, v in d.get("answers", {}).items()}

    state = {
        "name": d.get("name", "Human"),
        "answers": answers,
        "context_prompt": d.get("context_prompt", ""),
        "top_values": d.get("top_values", []),
        "chat_history": [
            {"role": m["role"], "content": m["content"]} for m in d.get("chat_history", [])
        ],
        "github_status": None,
    }
    RUNTIME[sid] = state
    STORES[sid] = store
    return state, store


# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route("/", methods=["GET"])
def intro():
    return render_template("intro.html")


@app.route("/start", methods=["POST"])
def start():
    name = request.form.get("name", "").strip() or "Human"
    sid = new_session(name)
    session["sid"] = sid
    return redirect(url_for("question", idx=0))


@app.route("/question/<int:idx>", methods=["GET"])
def question(idx):
    sid = session.get("sid")
    state, _ = get_state(sid)
    if state is None:
        return redirect(url_for("intro"))
    if idx < 0 or idx > 14:
        return redirect(url_for("question", idx=0))

    q = QUESTIONS[idx]
    answer = state["answers"].get(idx, "")
    pct = int((idx / 15) * 100)
    return render_template(
        "question.html",
        q=q, idx=idx, total=15, name=state["name"],
        answer=answer, pct=pct, is_last=(idx == 14),
    )


@app.route("/question/<int:idx>", methods=["POST"])
def question_post(idx):
    sid = session.get("sid")
    state, store = get_state(sid)
    if state is None:
        return redirect(url_for("intro"))

    q = QUESTIONS[idx]
    answer = request.form.get("answer", "").strip()
    state["answers"][idx] = answer
    store.save_answer(idx, q["theme"], q["q"], answer)

    action = request.form.get("action", "next")
    if action == "back" and idx > 0:
        return redirect(url_for("question", idx=idx - 1))
    if action == "generate" and idx == 14:
        scores, evidence = score_answers(state["answers"])
        prompt, top5 = assemble_context_prompt(state["name"], scores, evidence, state["answers"])
        state["context_prompt"] = prompt
        state["top_values"] = top5
        store.save_context(prompt, top5)

        # Push the completed session (all answers + generated prompt) to GitHub.
        session_data = store.read() or {}
        state["github_status"] = push_session_to_github(sid, session_data)

        return redirect(url_for("result"))
    if idx < 14:
        return redirect(url_for("question", idx=idx + 1))
    return redirect(url_for("question", idx=idx))


@app.route("/result", methods=["GET"])
def result():
    sid = session.get("sid")
    state, store = get_state(sid)
    if state is None or not state.get("context_prompt"):
        return redirect(url_for("intro"))
    return render_template(
        "result.html",
        name=state["name"],
        top_values=state["top_values"],
        context_prompt=state["context_prompt"],
        chat_history=state["chat_history"],
        session_file=store.path.name if store.path else "",
        github_status=state.get("github_status"),
        github_configured=github_configured(),
    )


@app.route("/restart", methods=["POST"])
def restart():
    sid = session.get("sid")
    if sid:
        RUNTIME.pop(sid, None)
        STORES.pop(sid, None)
    session.clear()
    return redirect(url_for("intro"))


@app.route("/api/chat", methods=["POST"])
def api_chat():
    sid = session.get("sid")
    state, store = get_state(sid)
    if state is None or not state.get("context_prompt"):
        return jsonify({"error": "No active session. Please restart."}), 400

    data = request.get_json(silent=True) or {}
    user_text = (data.get("message") or "").strip()
    if not user_text:
        return jsonify({"error": "Empty message."}), 400

    state["chat_history"].append({"role": "user", "content": user_text})
    store.save_chat_turn("user", user_text)

    try:
        client = anthropic.Anthropic()
        messages = [{"role": m["role"], "content": m["content"]} for m in state["chat_history"]]
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1000,
            system=state["context_prompt"],
            messages=messages,
        )
        reply = resp.content[0].text
        state["chat_history"].append({"role": "assistant", "content": reply})
        store.save_chat_turn("assistant", reply)
        return jsonify({"reply": reply})
    except Exception as e:
        err = f"[Error: {e}]"
        state["chat_history"].append({"role": "assistant", "content": err})
        store.save_chat_turn("assistant", err)
        return jsonify({"error": str(e)}), 500


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
