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

This is fine for exploring and demoing the app. Before importing your real 15,600 contacts, we should add a persistent disk (a small paid add-on on Render) or migrate storage to a real database.

## Running locally instead

```bash
pip install -r requirements.txt
python app.py
```

Then open `http://localhost:5000` in your browser.
