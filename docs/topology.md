# Public hostname topology

> **Audience:** Operators deciding how to expose their Studio instance to the public internet.

This page explains the two ways `studio-console` can wire up your public hostnames, why you'd pick one over the other, and how to debug it when something goes wrong.

You don't have to read this front-to-back. Use the table of contents to jump to the section that matches your situation.

- [What the wizard asks you](#what-the-wizard-asks-you)
- [Single hostname (default)](#single-hostname-default)
- [Split hostnames (UI + API)](#split-hostnames-ui--api)
- [Choosing IP restrictions](#choosing-ip-restrictions)
- [Apex routing (`example.com` with no subdomain)](#apex-routing-examplecom-with-no-subdomain)
- [Conflict prompts during the wizard](#conflict-prompts-during-the-wizard)
- [Why does the UI hostname proxy `/api/*`?](#why-does-the-ui-hostname-proxy-api)
- [What the console manages — and what it doesn't](#what-the-console-manages--and-what-it-doesnt)
- [Troubleshooting](#troubleshooting)

---

## What the wizard asks you

The Public Access section of the wizard takes a few short prompts to build your hostname configuration:

1. **Root domain.** The bare domain you own — `example.com`. This must be a zone in your Cloudflare account.
2. **UI subdomain.** A subdomain for the UI — `app`, `studio`, `www`, whatever fits. Required (apex/root URLs are not configured by the wizard — see [Apex routing](#apex-routing-examplecom-with-no-subdomain)). Combines with the root to make `https://<sub>.<root>` for your UI.
3. **Separate API hostname?** Yes/no. Yes is recommended if you want webhooks, OAuth callbacks, or 3rd parties to reach your API while keeping the UI private.
4. **Public API hostname (menu).** Pick from a short list of suggestions based on your UI hostname. The first option is always `api.<root>` — the canonical answer most operators want. There's also a "Custom" option for unusual setups.
5. **IP allowlist scope.** None / UI only / Both — see [Choosing IP restrictions](#choosing-ip-restrictions).

The wizard suggests a Cloudflare Access app name based on your UI subdomain (`Studio - app`, `Studio - app-mac`, etc.) so multiple environments under one Cloudflare account stay distinguishable. You can override the suggestion at the prompt.

---

## Single hostname (default)

Everything runs under one hostname — for example `https://app.example.com`. The browser, webhooks, and OAuth providers all hit the same address. Nginx, sitting behind the Cloudflare tunnel, looks at the path of each request and forwards it to the UI or the API as appropriate.

```
                                ┌───────────────────────────────┐
                                │  app.example.com              │
                                │  (one Cloudflare Access app)  │
                                └───────────────┬───────────────┘
                                                │
                                                ▼
                              ┌──────────────────────────────────┐
                              │  nginx                           │
                              │  /api/*, /ws/*, /uploads/*  → API│
                              │  /                          → UI │
                              └──────────────────────────────────┘
```

**Pick this if:**
- You're the only person using Studio, or your team is on a shared IP/VPN.
- You don't need OAuth, webhooks, or any 3rd-party service to call back into Studio.
- You want the simplest possible setup.

**Limitation:** any IP restriction you put on the public hostname applies to *everything*. If you allowlist your home IP, OAuth callbacks from Google or webhooks from Stripe will be blocked too — they don't come from your IP.

---

## Split hostnames (UI + API)

The UI and the API get **separate public hostnames** — for example `https://app.example.com` for the UI and `https://api.example.com` for the API. Each hostname is a separate Cloudflare Access application, so you can put different access policies on them.

```
       ┌───────────────────────────┐         ┌──────────────────────────┐
       │  app.example.com          │         │  api.example.com         │
       │  (UI Access app)          │         │  (API Access app)        │
       └─────────────┬─────────────┘         └─────────────┬────────────┘
                     │                                     │
                     ▼                                     ▼
                                ┌──────────────┐
                                │  nginx       │
                                │  splits by   │
                                │  server_name │
                                └─────┬────────┘
                                      │
                              UI ◄────┴────► API
```

**Pick this if:**
- You want to lock the UI to your own IP(s) but let public services (OAuth, webhooks, integrations) reach the API.
- You expect to give external partners or scripts API access without exposing the UI.

**How it's wired:**
- The UI hostname's nginx block proxies `/api/*`, `/ws/*`, and `/uploads/*` to the API service. Browser sessions hit the API same-origin through the UI hostname — see [why](#why-does-the-ui-hostname-proxy-api).
- The API hostname's nginx block sends *everything* to the API. There's no UI behind this hostname.
- Both hostnames go to the same `studio-api` service inside Docker. The split is in *who can reach which hostname* (Cloudflare Access policy) — not in which API gets called.

**Defaults the wizard suggests:** if your UI is `https://app.example.com`, the wizard suggests `https://api.example.com` for the API. You can override.

---

## Choosing IP restrictions

When you configure Cloudflare during the wizard, you'll be asked whether to restrict access by IP. The options depend on whether you set up split hostnames.

### Single hostname

| Option | What it does | When to pick |
|--------|--------------|--------------|
| **No**  | Anyone with the URL can reach Studio (still gated by Cloudflare TLS, but no IP filter). | You want to log in from anywhere. |
| **Yes** | Only listed IPs can reach Studio at all. OAuth/webhooks **will not work** because their IPs aren't in your list. | You're locking down access for yourself or a known team. |

### Split hostnames

| Option | UI hostname | API hostname | When to pick |
|--------|-------------|--------------|--------------|
| **No**       | public      | public       | Both are open. Useful for staging / demo. |
| **UI only**  | IP-gated    | public       | Recommended. You log in from your IP; webhooks/OAuth/integrations work. |
| **Both**     | IP-gated    | IP-gated     | You want everything private and don't need 3rd parties to call your API. |

You can change this later from `studio-console → Cloudflare → Update IP rules`. Edits apply to whichever app(s) you select; in split mode you can pick UI, API, or both.

---

## Apex routing (`example.com` with no subdomain)

The wizard configures hostnames *under* your root domain (e.g. `app.example.com`, `api.example.com`). It never touches the apex (`example.com` itself). So a visitor who types just your root domain into a browser will land on whatever the apex points to — typically nothing, or a 404 from Cloudflare.

If you want the apex to redirect to your UI, set up a **Cloudflare Bulk Redirect** (or a Page Rule on legacy plans) manually in the Cloudflare dashboard:

1. In the Cloudflare dashboard, go to **Rules → Redirect Rules** (or **Bulk Redirects**).
2. Create a redirect: `https://example.com/*` → `https://<your-ui-hostname>/$1` with status 301.
3. Save. The redirect is active immediately.

The console intentionally does not manage this for you — apex redirects are a single-line setting in Cloudflare and operators sometimes have other plans for the apex (a marketing landing page, redirecting to a different domain, etc.).

---

## Conflict prompts during the wizard

The wizard tries hard not to silently overwrite resources you (or someone else) configured manually in Cloudflare. You'll see a confirmation prompt when:

| Prompt | What happened | What to do |
|--------|---------------|------------|
| `<host> already has DNS records that aren't pointing at this tunnel: …` | The hostname you picked already has a DNS record (A record to a server, CNAME to something else, etc.). | Confirm to overwrite, or cancel and pick a different subdomain. |
| `An Access app named '…' already exists.` | A Cloudflare Access app with the suggested name already exists — usually because you re-ran the wizard, or another environment shares the name. | "Use the existing app" if it's yours, or "Pick a different name" to keep them separate. |
| `A tunnel named '…' already exists.` | Same idea, for the Cloudflare tunnel. | "Use the existing tunnel" reattaches; "Choose a different name" creates a new one. |

If you see these prompts on a fresh install, something else is using the same names — check what's already in your Cloudflare account before overwriting.

---

## Why does the UI hostname proxy `/api/*`?

This trips most people up the first time. Short version: **the wizard configures the browser running the UI to call the API same-origin — on the UI hostname.**

When a user opens `https://app.example.com`, the JavaScript in the page makes calls like `fetch('/api/v1/orgs')`. That URL resolves to `https://app.example.com/api/v1/orgs`. So nginx on the UI hostname has to know to forward `/api/*` to the API service — otherwise Next.js catches it and returns a 404. The same applies to `/uploads/...` (org media) and `/ws/...` (WebSockets).

This is a **wizard choice**, not a topology guarantee. The wizard sets `SHS_API_BASE_URL` to the UI hostname so the UI calls the API same-origin. If you change `SHS_API_BASE_URL` in `.env` to point to the API hostname directly, the UI will start making cross-origin calls — and you'll need to think about CORS, cookies, and the security implications below.

Why is same-origin the default?

- The **IP allowlist on the UI hostname keeps protecting the API**. Browser sessions go through the UI hostname, so the gate applies. If you flipped to cross-origin, browser API calls would bypass the UI's IP rule — defeating the point of "UI only" mode.
- Same-origin auth cookies and CSRF tokens "just work." Cross-origin requires `credentials: 'include'`, explicit `Access-Control-Allow-Credentials`, and care around SameSite — a classic source of subtle auth bugs.
- CORS preflights aren't issued, so there's nothing to misconfigure in `SHS_CORS_ORIGINS` for browser traffic. (External callers hitting the API hostname directly are not subject to CORS — it's a browser-only enforcement.)

So the design is deliberate: **two routes to the API, two policies**.

| Caller | Reaches API via | Gated by |
|--------|-----------------|----------|
| Logged-in browser session | UI hostname (`/api/*`, `/ws/*`, `/uploads/*`) | UI Access policy (your IP allowlist, if set) |
| Webhook / OAuth callback / 3rd-party script | API hostname | API Access policy (typically none = public) |

If you're configuring the API from the outside (Postman, curl, a webhook source), use the **API hostname**. If you're loading the UI in your browser, the API calls happen automatically through the **UI hostname** — you don't need to think about it.

### Note for those familiar with dev

Dev runs a different topology: cloudflared on the host with one tunnel route per service, no nginx in the path. The dev UI at `https://app.self-hoststudio.com` calls the API at `https://api.self-hoststudio.com` *cross-origin*, because `SHS_API_BASE_URL` is set to the API hostname there. CORS is exercised in dev and not in prod-split. A `SHS_CORS_ORIGINS` typo or omission shows up loudly in dev and is silent in prod — don't dismiss CORS errors in dev as "prod won't care."

---

## Troubleshooting

### `403 Forbidden` from Cloudflare

Cloudflare Access is blocking the request before it reaches your server.

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| 403 when loading the UI from a new location | UI Access policy IP rule doesn't include this IP | Update IP rules: `studio-console → Cloudflare → Update IP rules` |
| 403 when an OAuth provider tries the callback URL | You're in single-hostname mode with IP restriction, *or* in split mode with `Both` IP restriction | Switch to split hostnames + `UI only` IP restriction |
| 403 when a webhook tries to call the API | Same as above | Same as above |

### `404 Not Found` on `/uploads/...` or `/orgs/...`

Nginx isn't routing the path to the API service, *or* the API doesn't have anything to serve there.

- **Single hostname mode:** make sure nginx's `location` block includes `/uploads/`. Re-run the wizard; the current console emits the correct rule.
- **Split mode:** the API hostname routes everything to the API by default — `/uploads/...` should work there directly. If you're seeing this on the UI hostname, the `/uploads/` location is missing from the UI server block. Re-running the wizard will regenerate it.
- **API has no workspace dir:** the API mounts `/uploads/orgs` from the workspace at startup. If the workspace directory doesn't exist, the mount is skipped and every `/uploads/...` request 404s regardless of nginx. Check the API container logs for either `Mounted workspace uploads at /uploads/orgs -> ...` (good) or a warning that the directory was missing (bad).

### Quick verification after deploy

```sh
# Should return 200 (image bytes), provided the file exists.
curl -I https://app.example.com/uploads/orgs/<id>/instances/<id>/<file>.png
curl -I https://api.example.com/uploads/orgs/<id>/instances/<id>/<file>.png
```

In split mode both should work. The first proves nginx is forwarding `/uploads/` correctly on the UI hostname; the second proves the FastAPI static mount is alive on the API hostname.

You can also open an instance result image in the UI, then check the `<img src>` in DevTools. That tells you which hostname `SHS_API_BASE_URL` is resolving to and confirms whether traffic is same-origin or cross-origin.

### Image URLs in the UI are pointing to the wrong hostname

Images served from the API land on whatever hostname the API code thinks is its public URL — set by `SHS_PUBLIC_BASE_URL` (UI hostname). That's intentional in single-hostname mode, and it's fine in split mode too because the UI hostname's nginx forwards `/uploads/*` to the API.

If you're seeing images served from the wrong hostname (e.g. an internal Docker name like `nginx:80` leaking out), check that the API container is starting with `SHS_PUBLIC_BASE_URL` set correctly in `.env`.

### "I changed `CONSOLE_IP_RESTRICT_MODE` in `.env` and nothing happened"

The `.env` value is read by the wizard, not by Cloudflare. Cloudflare Access policies are set when you run `studio-console → Cloudflare → Full setup` or `Update IP rules`. Editing `.env` alone does not push the change to Cloudflare.

### Switching from split mode back to single hostname

The wizard supports turning off the separate API hostname — run the Public access section again and answer "no" to the separate-API-hostname prompt.

What the console **does** when you do this:
- Removes `CONSOLE_PUBLIC_API_BASE_URL` from `.env`.
- Re-pushes the tunnel ingress with only the UI hostname.

What the console **does not** do:
- Delete the API Access app in Cloudflare (you may want to keep it for later).
- Delete the API DNS record.

If you want a clean slate, delete those manually in the Cloudflare dashboard after the wizard finishes.

---

## What the console manages — and what it doesn't

Cloudflare access is a security boundary. The console takes a deliberately narrow role here: it creates the resources needed to bootstrap a working install, and from then on the **security policy is yours to manage**. The console will not silently delete tunnels, Access apps, or DNS records that you (or it) created earlier — even when the wizard's input suggests doing so might be "convenient."

### What the console creates

- A **Cloudflare tunnel** named `studio` (overridable) the first time you run setup with API credentials but no tunnel ID.
- A **DNS CNAME** for each public hostname pointing at the tunnel.
- A **Cloudflare Access app** (named `Studio - <subdomain>`) for each hostname *that needs IP gating* (`CONSOLE_IP_RESTRICT_MODE` ≠ `none`). Hostnames that should be public get **no Access app at all** — an Access app with no policy blocks everyone, so creating one for a public hostname would defeat the purpose.
- An **IP bypass policy** named `Studio Console - IP Bypass` on each gated app.

The console only ever modifies policies it created (matched by name). Any other policies you've added to those apps in the Cloudflare dashboard are left alone.

### What the console does NOT do

- **It will not delete tunnels.** If you stop using a tunnel — by removing the public domain, switching tunnels, or uninstalling — the old tunnel remains in your Cloudflare account. Delete it manually in the dashboard if you want it gone.
- **It will not delete Access apps.** Same rationale. If you remove an IP rule and the operator wants the hostname to become public again, the console **refuses** to leave an Access app with no policy (which would block everyone) — instead it warns and points you to the dashboard to delete the app yourself.
- **It will not delete DNS records.** When you change a hostname via "Update domain," the console offers to delete the old DNS CNAME and overwrites it. But unrelated DNS records on the same hostname trigger a confirmation prompt rather than silent overwrite.
- **It will not modify policies you created in the dashboard.** Only `Studio Console - IP Bypass` is touched. Custom policies (SSO, MFA, country block, mTLS, etc.) you've layered on are preserved.

### Why this matters

Two reasons:

1. **Security configurations are intentional.** A removed IP rule probably means "I want this public" — but it could also mean "I'm replacing it with an SSO policy I'll add manually." The console can't tell the difference, so it errs on the side of not removing security controls without an explicit instruction.

2. **The console isn't a Cloudflare management tool.** It's an installer that uses Cloudflare to make Studio reachable. Using it as the primary way to configure your Cloudflare account would mean the console has to track and own everything in your account — that's a much bigger commitment than what this tool is for.

Practical implication: when you tear down a Studio install, you have a small list of manual cleanup steps in the Cloudflare dashboard. Your reset script should include them:

- Delete the tunnel (named `studio` by default)
- Delete the Access app(s) (`Studio - <subdomain>`)
- Delete the DNS CNAME(s) you set up

---

## Going further

The console wizard handles the common cases. For more exotic setups (multi-zone, Bring-Your-Own-Tunnel, custom Access policies beyond IP allowlists), use the Cloudflare dashboard directly — the console is designed to leave existing resources alone if you've manually customised them, but it cannot undo or merge external edits, so changes you make in the dashboard may need to be re-applied after running the wizard.
