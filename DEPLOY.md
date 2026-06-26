# Deploying the bot 24/7 (free, no credit card)

This guide gets the bot running in the cloud so it trades on its own, around the
clock, without your PC being on — using only **free, no-card** services.

**Recommended path: Render (free) + UptimeRobot (free).**
Neither requires a credit card. You get the live dashboard, the 1-minute trading
loop, and Telegram alerts.

---

## Part A — Get your Telegram credentials (2 min)

You'll need these so the cloud bot can message your phone.

1. In Telegram, open **@BotFather** → send `/newbot` → follow prompts → copy the
   **token** (looks like `8123456:AAH...`).
2. Open **@userinfobot** → send `/start` → copy your numeric **Id** (the chat id).
3. Open your new bot's chat and press **Start** once (so it's allowed to message you).

Keep both handy for Part B.

---

## Part B — Deploy on Render (free, no card)

1. Go to <https://render.com> and **Sign up with GitHub** (no card asked).
2. Click **New +** → **Blueprint**.
3. Connect and pick your repo **`Top-Gainers-Strategy-By-Nik`**.
   Render reads `render.yaml` and sets everything up automatically.
   *(If Blueprint isn't offered: New + → **Web Service** → pick the repo →
   Runtime **Python 3**, Build `pip install -r requirements.txt`, Start
   `python main.py`, Plan **Free**.)*

   > 🌏 **CRITICAL — pick a non-US region: `Singapore` (or `Frankfurt`).**
   > Binance **blocks US IP addresses** (you'll get `HTTP 451` and the bot can't
   > fetch any market data). The `render.yaml` already requests **Singapore**.
   > If you create the service manually, set the **Region** dropdown to Singapore
   > yourself. Region can't be changed later — if your service is already in a US
   > region, **delete it and recreate it in Singapore.**
4. Before the first deploy, open **Environment** and add two variables:
   - `TELEGRAM_BOT_TOKEN` = your BotFather token
   - `TELEGRAM_CHAT_ID` = your numeric id
5. Click **Create / Deploy** and wait ~2–3 min for the build.
6. When it's live, Render gives you a URL like
   `https://top-gainers-bot.onrender.com`. Open it → you should see the
   **dashboard**, and a **“Bot started”** message should hit your Telegram.

> 💳 If Render ever asks for a card, stop here and tell me — we'll switch to the
> **GitHub Actions** fallback (Part D), which is 100% card-free.

---

## Part C — Keep it awake with UptimeRobot (free, no card)

Render's free instance sleeps after 15 min of no traffic. UptimeRobot pings it
so it never sleeps.

1. Go to <https://uptimerobot.com> → sign up (no card).
2. **Add New Monitor** → Type **HTTP(s)** → URL = your Render URL → interval
   **5 minutes** → Create.

That's it — the bot now runs 24/7 and trades on its own.

> ⚠️ **Persistence note:** Render's *free* tier has no permanent disk, so on a
> redeploy the SQLite history resets to $100. To keep data **forever**, connect a
> free Neon Postgres database — see **Part F** below.

---

## Part D — Fallback: GitHub Actions (guaranteed card-free)

If Render won't work without a card, the bot can run on a schedule directly from
GitHub — no server, no card. Trade-offs: it checks every ~10 minutes (not every
minute) and there's no hosted dashboard (run it locally when you want to look).
Tell me and I'll add the workflow.

---

## Part E — Any server you control (VPS / home box / Raspberry Pi)

If you ever get a server, it's one command (Docker required):

```bash
git clone https://github.com/NikShrestha/Top-Gainers-Strategy-By-Nik.git
cd Top-Gainers-Strategy-By-Nik
cp .env.example .env        # then edit .env with your Telegram token + chat id
docker compose up -d --build
```

The dashboard is on port 8000; the database persists in `./data`; the bot
auto-restarts on crash or reboot.

---

## Part F — Permanent data with Neon (free, no card)

By default the bot stores everything in a SQLite file, which Render wipes on
redeploy. Connect a **free Neon Postgres** database and your balance + trade
history survive forever. The bot auto-detects it — set one env var and you're done.

1. Go to <https://neon.tech> → **Sign up with GitHub** (free, no card).
2. Create a project (it makes a database automatically).
3. On the project dashboard, click **Connect** / **Connection string** and copy it.
   It looks like:
   `postgresql://user:password@ep-xxxx.region.aws.neon.tech/dbname?sslmode=require`
4. In Render → your service → **Environment** → add:
   - `DATABASE_URL` = that whole connection string
5. **Save changes.** Render redeploys, and the bot now reads/writes Neon. From
   now on, redeploys keep all your data.

That's it — no code changes needed. To go back to local SQLite, just remove the
`DATABASE_URL` variable.

> Tip: if you want to wipe and start fresh later, use the dashboard **Admin →
> Reset balance** button.

## After it's deployed

- Watch the **dashboard** and **Telegram** for a few days.
- We then move to **Phase 8**: review the results and tune thresholds in
  `config.py` (especially comparing flat-base vs non-flat-base win rates).
