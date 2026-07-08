# Soul Prompt (luxury edition) — Flask app

A 15-question, free-text values interview. Answers are scored across 12 value dimensions with a local keyword/TF-style algorithm (no API call needed for scoring), which assembles an "agent context prompt." You then chat with a Claude agent that uses that prompt as its system prompt.

Converted from a Jupyter/ipywidgets notebook into a standard Flask web app so it can be deployed anywhere, including Railway.

## Project layout

```
soul-prompt-luxury-flask/
├── app.py                  # Flask app: routes, scoring engine, session persistence
├── templates/
│   ├── base.html            # shared layout + CSS (gold/serif "Soul Prompt" theme)
│   ├── intro.html           # name entry screen
│   ├── question.html        # one question at a time, free-text textarea + "why" note
│   └── result.html          # dominant values + generated prompt + live chat widget
├── requirements.txt
├── Procfile                 # gunicorn start command (used by Railway)
├── runtime.txt               # pinned Python version
├── .env.example
└── .gitignore
```

## How it works

- Each visitor gets a session id in a signed cookie. Answers, the generated prompt, and chat history are kept server-side (in memory, plus a JSON file per session under `soul_prompt_sessions/` so state survives a process restart).
- The questionnaire is a plain HTML form with a `<textarea>` per question — no JavaScript needed to answer or navigate.
- The chat widget on the result page uses a small amount of vanilla JS to call `POST /api/chat`, which calls the Anthropic API with the generated prompt as the `system` message.

## Local development

```bash
cd soul-prompt-luxury-flask
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY and SECRET_KEY

export $(grep -v '^#' .env | xargs)   # or use python-dotenv / your shell's env loading
python app.py
```

Visit `http://localhost:5000`.

## Deploying to Railway

### Option A — Railway CLI

```bash
cd soul-prompt-luxury-flask
npm install -g @railway/cli    # if you don't have it already
railway login
railway init
railway up
```

Then set the required environment variables:

```bash
railway variables --set "ANTHROPIC_API_KEY=sk-ant-..." --set "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

Railway auto-detects Python from `requirements.txt` and uses the `Procfile` to run `gunicorn app:app`. It also injects `PORT` automatically, which the Procfile already binds to.

### Option B — Railway dashboard (GitHub deploy)

1. Push this folder to a GitHub repo.
2. In Railway: **New Project → Deploy from GitHub repo**, select the repo.
3. Railway detects the `Procfile`/`requirements.txt` and builds it as a Python (Nixpacks) service automatically — no Dockerfile needed.
4. Under **Variables**, add:
   - `ANTHROPIC_API_KEY` — your Anthropic API key
   - `SECRET_KEY` — any long random string
   - (optional) `ANTHROPIC_MODEL` if you want to override the default model
5. Click **Deploy**. Railway will give you a public `*.up.railway.app` URL.

### Notes on persistence

Railway's default filesystem is ephemeral — it resets on every redeploy (though it persists across restarts/scaling of the same running instance). The JSON session files under `soul_prompt_sessions/` are written for convenience/debugging, not as a database. If you need answers and chat transcripts to survive redeploys, either:

- attach a [Railway volume](https://docs.railway.com/reference/volumes) and point `SESSIONS_DIR` at it, or
- swap `SessionStore` in `app.py` for a real database (Postgres, Redis, etc.) — Railway can provision either with one click.

The app keeps live session state in an in-memory dict, which assumes a single running instance. If you scale to multiple replicas, move session state to Redis or a database so all instances see the same data.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | yes | — | Used implicitly by the `anthropic` SDK to call Claude in `/api/chat` |
| `SECRET_KEY` | yes (in prod) | `dev-secret-change-me-in-production` | Signs the Flask session cookie |
| `ANTHROPIC_MODEL` | no | `claude-sonnet-4-6` | Model used for the chat widget |
| `SESSIONS_DIR` | no | `soul_prompt_sessions` | Where per-session JSON files are written |
| `PORT` | no (set by Railway) | `5000` | Port the app binds to |
