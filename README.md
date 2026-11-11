# IPTVV Canada Trial Automation — Apify Actor

Automates the IPTVV.ca free-trial WooCommerce checkout, waits for the
credentials email in a disposable inbox, extracts the Xtream credentials, and
reports them to the Apify dataset / `OUTPUT` key-value record (and optionally to
a webhook and IBO Player).

## Layout

| Path | Purpose |
| --- | --- |
| `iptvvcanada_automation.py` | Core automation (also runnable as a CLI). `run_automation()` returns the credentials. |
| `browser/checkout.js` | Puppeteer worker (`puppeteer-extra` + stealth plugin) that drives the checkout in real Chrome. Python talks to it over JSON lines. |
| `puppeteer_client.py` | Python client that starts the worker and sends it commands. |
| `src/main.py` | Apify wrapper: input → env vars → `run_automation()` → dataset/OUTPUT/webhook. |
| `src/__main__.py` | Entry point (`python -m src`). |
| `.actor/actor.json` | Actor definition. |
| `.actor/input_schema.json` | Input fields shown in the Apify console. |
| `.actor/dataset_schema.json` | Dataset table view. |
| `Dockerfile` | Built on `apify/actor-python-selenium` (Chrome preinstalled), plus Node.js for the Puppeteer worker. |
| `requirements.txt` | Python dependencies. |

## Deploy to Apify

```bash
# 1. Install the Apify CLI (once)
npm install -g apify-cli

# 2. Log in (opens a token prompt — get the token from
#    https://console.apify.com/account/integrations)
apify login

# 3. From this directory, push the Actor (builds the Docker image on Apify)
apify push
```

`apify push` uploads the source, builds the image, and creates/updates the Actor
in your account. After it builds, run it from the console or with
`apify call iptvv-canada-trial`.

## Input

All fields are optional (see `.actor/input_schema.json`). Common ones:

- `twoCaptchaApiKey` — 2captcha key for solving the checkout reCAPTCHA.
- `callbackUrl` / `webhookAuthToken` — POST the result to your backend.
- `emailBackend` — `procmail` (default) or `mailtm`.
- `iboPlayerEnabled` + `iboPlayerCookie` + `iboPlayerPlaylistUrlId` — push the
  playlist to IBO Player.

### Gmail backend with your own domain

IPTVV treats Gmail plus-aliases (`you+123@gmail.com`) as the same address as
`you@gmail.com`, so they only ever get one trial. Use a domain you own instead:

1. In Cloudflare, open the domain → **Email** → **Email Routing** → **Routing rules**.
2. Enable the **Catch-all address** with action *Send to an email* → your Gmail
   (it must be a verified destination address).
3. Set the Actor input: `emailBackend` = `gmail`, `gmailAddress`,
   `gmailAppPassword` (a Google app password), and `gmailAliasDomain` =
   `yourdomain.com`.

Each run then checks out with a fresh address like `emma.taylor4821@yourdomain.com`
and reads the credentials email from the Gmail inbox (INBOX and Spam) over IMAP.

## Output

On success the Actor pushes one dataset item and sets the `OUTPUT` record:

```json
{ "status": "success", "email": "...", "username": "...", "password": "...", "host": "...", "m3u_url": "..." }
```

On failure it records `{ "status": "failed", "error": "..." }` and the run is
marked failed.

## Run locally (without Apify)

```bash
pip install -r requirements.txt
(cd browser && npm ci)                      # Node >= 18; uses your installed Google Chrome
python iptvvcanada_automation.py            # full run
python iptvvcanada_automation.py --preflight-only
```
