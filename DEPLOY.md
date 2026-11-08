# Deploying the Quotation Manager

The app is ready to host. Two settings matter when it is online:

| Setting (environment variable) | What it does |
|---|---|
| `APP_PASSWORD` | **Required online.** Everyone must enter this password. Without it, anyone with the link can read and edit your quotations. |
| `DATA_DIR`     | Folder where `quotations.db` is stored. It must be on **persistent storage**, otherwise saved quotations, customers and numbering are lost when the server restarts. |

Start command (Linux hosts): `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60`

## Option A - PythonAnywhere (simplest, low cost, files persist)
1. Create an account at pythonanywhere.com and open a **Bash console**.
2. Upload and unzip `quotation_app.zip` (Files tab), then:
   `cd quotation_app && pip install --user -r requirements.txt`
3. **Web** tab -> *Add a new web app* -> *Manual configuration* -> pick Python 3.10+.
4. Set *Source code* to `/home/<username>/quotation_app`.
5. Open the *WSGI configuration file* and replace its contents with:
   ```python
   import os, sys
   sys.path.insert(0, "/home/<username>/quotation_app")
   os.environ["APP_PASSWORD"] = "choose-a-strong-password"
   os.environ["SECRET_KEY"] = "any-long-random-text"
   os.environ["DATA_DIR"] = "/home/<username>/quotation_data"
   from app import app as application
   ```
6. Click **Reload**. The app is live at `https://<username>.pythonanywhere.com` (HTTPS included).

## Option B - Render.com (uses the included render.yaml)
1. Put this folder in a GitHub repository.
2. Render -> New -> *Blueprint* -> select the repository.
3. Enter `APP_PASSWORD` when asked. The blueprint adds a 1 GB persistent disk
   (needs a paid instance type; the free type has no persistent disk).

## Option C - Any server / VPS with Docker
    docker build -t quotations .
    docker run -d -p 80:8000 -e APP_PASSWORD=yourpassword -v quotation_data:/data quotations
Put it behind HTTPS (e.g. Caddy or nginx with Let's Encrypt) before using it over the internet.

## Option D - A PC in your office (Windows)
    pip install -r requirements.txt
    set APP_PASSWORD=yourpassword
    waitress-serve --listen=0.0.0.0:5000 app:app
Colleagues open `http://<that-PC-ip>:5000`. Keep the PC on and give it a fixed IP.

## Backups
Everything is in one file: `quotations.db` inside `DATA_DIR`. Copy it regularly.
