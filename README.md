# Holetrainer — SMS Marketing App

## Deploy to Render (free tier)

1. **Create a GitHub repo** (github.com → New repository → name it `holetrainer`).
2. **Upload this project** to that repo (drag-and-drop all these files on the GitHub web page, or use `git push` if you're comfortable with git).
3. **Go to [render.com](https://render.com)** → sign up (free) → **New +** → **Web Service**.
4. **Connect your GitHub repo** (Render will ask for permission the first time).
5. Render should auto-detect Python. Confirm these settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT` (already in `Procfile`, Render usually picks it up automatically)
6. Under **Environment Variables**, add:
   - `SECRET_KEY` → any long random string (e.g. generate one at randomkeygen.com)
   - `FLASK_DEBUG` → `false`
7. Click **Create Web Service**. Wait ~2 minutes for the first deploy.
8. You'll get a live URL like `https://holetrainer.onrender.com` — open it, and you'll see the **Set up Holetrainer** screen to create your admin account.

## Important: free tier storage is temporary

On Render's free tier, the filesystem resets whenever the service restarts or redeploys (including automatic sleep after inactivity). That means contacts, templates, and campaigns saved as local files will be **wiped** on restart.

This is fine for exploring and demoing the app. Before importing real contacts, add a persistent disk (see below).

## Adding persistent storage (do this before importing real contacts)

Render's persistent disks require a **paid instance type** (Starter or above, ~$7/month). This is also worth doing anyway because paid instances don't spin down from inactivity — important since campaign sends now run in the background and a spun-down free instance would interrupt a long send.

1. In the Render dashboard, open your `holetrainer` service.
2. Go to **Settings** → find **Instance Type** → upgrade from "Free" to "Starter" (or any paid tier).
3. Go to the **Disks** tab (only visible on paid instances) → **Add Disk**.
   - **Name:** `holetrainer-data`
   - **Mount Path:** `/var/data`
   - **Size:** 1 GB is plenty for contacts/templates/campaigns as CSV files.
4. Go to **Environment** → add these two variables:
   - `DATA_DIR` → `/var/data/data`
   - `LOGS_DIR` → `/var/data/logs`
5. Save. Render will redeploy automatically — the app will create the folders on the disk the first time it starts, and everything saved from then on survives restarts and redeploys.

## Running locally instead

```bash
pip install -r requirements.txt
python app.py
```

Then open `http://localhost:5000` in your browser.
