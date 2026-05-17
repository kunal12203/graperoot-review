# GitHub App Setup — GrapeRoot Review

Follow these steps to register the GitHub App and deploy the webhook server.

---

## 1. Register the GitHub App

Go to: https://github.com/organizations/YOUR_ORG/settings/apps/new
(or https://github.com/settings/apps/new for a personal app)

Fill in:

| Field | Value |
|-------|-------|
| **App name** | GrapeRoot Review |
| **Homepage URL** | https://review.graperoot.dev |
| **Webhook URL** | https://review.graperoot.dev/webhook |
| **Webhook secret** | generate with: `openssl rand -hex 32` |

**Permissions (Repository):**
- Contents: Read
- Pull requests: Read & Write
- Issues: Read

**Subscribe to events:**
- Pull request

Click **Create GitHub App**.

---

## 2. Generate a private key

On the App settings page → **Generate a private key**.
Save the `.pem` file — you'll need it as `GITHUB_PRIVATE_KEY`.

---

## 3. Deploy to Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app)

Set these environment variables in Railway:

| Variable | Where to find it |
|----------|-----------------|
| `GITHUB_APP_ID` | App settings page → App ID (number) |
| `GITHUB_PRIVATE_KEY` | Contents of the `.pem` file (paste as-is) |
| `GITHUB_WEBHOOK_SECRET` | The secret you generated in step 1 |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com |

Deploy → Railway gives you a URL like `https://graperoot-review.up.railway.app`.
Update the **Webhook URL** in your GitHub App to this URL + `/webhook`.

---

## 4. Install the App on a repo

GitHub App settings → **Install App** → choose repos.

Every new PR in those repos will now get a GrapeRoot review automatically.

---

## Self-hosted (Docker)

```bash
docker run -d \
  -e GITHUB_APP_ID=123456 \
  -e GITHUB_PRIVATE_KEY="$(cat your-app.private-key.pem)" \
  -e GITHUB_WEBHOOK_SECRET=your-secret \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -p 8080:8080 \
  graperoot/review
```

Point your GitHub App's webhook URL to `http://your-server:8080/webhook`.

---

## PAT mode (simpler, no GitHub App)

Set `GITHUB_TOKEN` instead of App credentials. Reviews will post as your personal account.
Useful for testing or single-repo setups.

```bash
export GITHUB_TOKEN=ghp_...
export ANTHROPIC_API_KEY=sk-ant-...
python webhook.py
```
