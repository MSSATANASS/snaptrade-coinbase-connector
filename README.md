# SnapTrade + Coinbase Multi-User (FastAPI)

A web app (FastAPI) to register SnapTrade users and connect Coinbase accounts via the SnapTrade **Connection Portal**. It securely stores users, connections, and access tokens in a persistent JSON file (ideal for **Render Disk**).

## Requirements
- Python 3.10+ (recommended)
- SnapTrade Account and Credentials (Client ID + Consumer Key)

## Important Files
- app.py: FastAPI backend + Embedded Tailwind CSS UI
- snaptrade_users.json: Local persistence (on Render, this uses `/var/data/snaptrade_users.json`)
- render.yaml: 1-click deployment configuration for Render
- .env.example: Environment variables template

## Note on SnapTrade (Free Tier)
SnapTrade usually limits registration to **1 user** when using Personal Keys.
If you attempt to register another user and get the error:
`Personal keys can only register one user`
The application will display a friendly message asking you to reuse the existing user or upgrade your plan.

For convenience, the application initializes with a "default" user already loaded:
- `DEFAULT_USER_ID = "puto_pablo"`
- `DEFAULT_USER_SECRET = "0bb9efc3-a75a-482c-b577-fa2e0fac47d6"`

## Run Locally

1) Create a virtual environment and install dependencies:
```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

2) Create your `.env` file (DO NOT upload to Git) by copying the example:
```bash
copy .env.example .env
```

3) Edit `.env` and add your credentials:
```env
SNAPTRADE_CLIENT_ID=...
SNAPTRADE_CONSUMER_KEY=...
```

4) Start the server:
```bash
uvicorn app:app --reload
```

5) Open:
- Admin Dashboard: http://127.0.0.1:8000/
- Healthcheck: http://127.0.0.1:8000/health

## Client Login Flow
1) As an admin, go to `/dashboard/{user_id}` and click **"Generate Access Link"**.
2) Share the generated one-time link with your client.
3) The client opens the link, which logs them into the **Client Portal** (`/client/dashboard`) using a secure `HttpOnly` cookie.
4) The client can then click **"Open Connection Portal"** to link their Coinbase account directly.

## Deployment on Render.com (Step by Step)

### 1) Push the repository to GitHub
- Create a repo and upload these files.
- Do NOT upload `.env`.

### 2) Create the service on Render
1. On Render: **New +** → **Blueprint**
2. Select your repository
3. Render detects `render.yaml` and will create the web service with a persistent disk.

### 3) Configure Environment Variables on Render
In the service settings, go to **Environment** and add:
- `SNAPTRADE_CLIENT_ID`
- `SNAPTRADE_CONSUMER_KEY`

### 4) Verify Render Disk (Persistence)
The `render.yaml` creates a Disk:
- `mountPath: /var/data`
- The persistence file remains at: `/var/data/snaptrade_users.json`

This prevents users/connections from being deleted during redeployments.

### 5) Configure the Callback / Redirect in SnapTrade
Once you have your Render domain (e.g. `https://your-app.onrender.com`), make sure SnapTrade uses:
- Callback/Redirect: `https://your-app.onrender.com/callback`

## Unit Tests
Run:
```bash
python -m unittest -v
```
