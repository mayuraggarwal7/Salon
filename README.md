# Salon software with WhatsApp booking

A web app for running a salon, where clients book, view and cancel appointments
by chatting on **WhatsApp**. Staff use a browser dashboard for the diary, clients,
services, stylists, opening hours and a shared WhatsApp inbox.

Built with Django 5 and the official **WhatsApp Business Cloud API** (Meta). It does
not use unofficial WhatsApp Web automation, which can get numbers banned.

## Features

**For clients, on WhatsApp**
- Say "hi" to get a menu: *Book appointment · My appointments · Services & prices · Talk to the salon*
- Booking uses WhatsApp list and button messages: service → stylist (or "no preference") → day → free time → confirm
- Only real free times are offered, based on opening hours, service length and each stylist's existing bookings
- View upcoming appointments and cancel them
- "Talk to the salon" hands the chat to a person. The bot stays quiet until staff hand it back or the client types *menu*
- Confirmation, cancellation and reminder messages

**Anchor stylists: one dedicated person per client**
- Every client can have an **anchor stylist**. Imported clients get the stylist who served them most often
- In WhatsApp the menu says *Message Priya* and the booking list shows *Priya ⭐ – your stylist* first. The front desk defaults to the anchor too
- After booking, the bot offers add-ons that go with the service (*"Priya recommends adding Hair spa"*). One tap books it straight after the main appointment. If it doesn't fit, the request goes to the stylist to arrange

**Routing WhatsApp chats to staff, one thread per client**
- *Message my stylist* routes the chat to the anchor stylist. Staff can re-route a chat to anyone, or back to the front desk
- Stylists with **"Forward chats to my WhatsApp"** get client messages on their own phone, from the salon number. They swipe-reply to answer, and the reply reaches the client from the salon number. Commands: `#who`, `#done` (hand back to the bot), `#help`
- Every message (bot, front desk, or stylist from the web or their phone) is stored in the client's thread, labelled with who sent it
- Stylists can have their own login. They see their own clients, chats and schedule; managers see everything

**Past invoices → charge and upsell prompts**
- Import past invoices from CSV (Excel or any billing system), or add one by hand with a scan/PDF attached
- The schedule and **Checkout** screen show what the client paid last time vs today's list price (*"Last charged ₹280 on 1 Aug; list price is now ₹300. Mention the new price."*)
- Suggestions, labelled by type:
  - **Due**: services they buy regularly and are due for, based on their own visit rhythm (or the service's rebook interval)
  - **Cross-sell**: add-ons set on the service, plus services other clients often buy together (learned from invoices)
  - **Upsell**: the service's premium upgrade
- Checkout: tick suggested add-ons, adjust prices, charge. This records an invoice, so the history keeps growing
- **Offer on WhatsApp** sends the client a one-tap *Book it* offer, which opens booking with their stylist preselected
- Client page: lifetime spend, average bill, price history per service, all invoices

**For staff, in the browser**
- Today view: schedule, expected takings, WhatsApp bookings, clients waiting for a reply
- Appointment diary by day and stylist; create or edit bookings with a free-slot picker; mark as done, no-show or cancelled (cancelling notifies the client)
- Client list with visit history and notes
- WhatsApp inbox: every conversation, bot and staff messages, live refresh, reply box, "take over" and "hand back to bot"
- Manage services and prices (with cross-sell add-ons, premium upgrade and rebook interval), stylists (services, login, WhatsApp forwarding), and opening hours
- Django admin at `/admin/`

## Quick start

```bash
cd Salon
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo      # sample services, stylists, hours, and an admin/admin login
python manage.py runserver
```

Open http://localhost:8000 and log in with `admin` / `admin`. Change that password straight away.

Without WhatsApp credentials the app runs in **dry-run mode**: bot replies are saved
and shown in the inbox, but nothing is sent. You can try the bot by POSTing webhook
payloads to `/whatsapp/webhook/`; see `whatsapp/tests.py` for examples.

Run the tests with `python manage.py test`.

## Deploying to Render (shareable test URL)

The repo includes a Render Blueprint (`render.yaml` at the repo root). It creates the web app and a Postgres
database on Render's free plan.

1. Sign up at [render.com](https://render.com) with your GitHub account.
2. **New → Blueprint**, pick the `Salon` repository and the `main` branch.
3. Render asks for the values marked "you type this in":
   - `DJANGO_ADMIN_PASSWORD`: the password for the `admin` manager login. Choose a strong one.
   - `WHATSAPP_*`: leave blank for now (the app runs in dry-run mode); fill them in later under the service's
     **Environment** tab.
4. Click **Apply**. The first deploy takes a few minutes. Your URL is shown at the top of the service page, e.g.
   `https://salon-xxxx.onrender.com`.
5. Log in as `admin` with the password you chose. Sample services, stylists and hours are loaded
   (`SALON_SEED_DEMO=true`). Try *Import* with `sample_invoices.csv`.

Then point Meta's webhook at `https://<your-url>/whatsapp/webhook/`. The verify token is the generated
`WHATSAPP_VERIFY_TOKEN` value on the **Environment** tab.

Free plan limits, fine for testing but not for running the salon:
- The app sleeps after 15 minutes idle, so the first request after that takes about a minute. WhatsApp retries
  webhooks, so messages still arrive, just late.
- The free Postgres database expires after 30 days.
- Uploaded invoice scans are lost on each redeploy. Add a Render persistent disk mounted at `SALON_MEDIA_ROOT`,
  or use object storage, for real use.
- Reminders need `python manage.py send_reminders` on a schedule (a Render Cron Job, paid).

For production, move the web service and database to paid plans and set `SALON_SEED_DEMO=false` before you
enter real data.

Other hosts work the same way: run `build.sh`, start `gunicorn salon_project.wsgi:application`, and set
`DATABASE_URL`, `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=false`, `DJANGO_ALLOWED_HOSTS`,
`DJANGO_CSRF_TRUSTED_ORIGINS`, `DJANGO_ADMIN_USERNAME` and `DJANGO_ADMIN_PASSWORD`.

## Connecting WhatsApp

1. At [developers.facebook.com](https://developers.facebook.com/), create an app of type **Business** and add the **WhatsApp** product.
2. Under *WhatsApp → API Setup*, note the **Phone number ID** and create a **permanent access token** (a System User token with `whatsapp_business_messaging`).
3. Deploy the app on a public HTTPS URL.
4. Under *WhatsApp → Configuration → Webhook*, set:
   - Callback URL: `https://your-domain/whatsapp/webhook/`
   - Verify token: the same value as `WHATSAPP_VERIFY_TOKEN`
   - Subscribe to the **messages** field
5. Set the environment variables below and restart.

| Variable | Purpose |
| --- | --- |
| `WHATSAPP_TOKEN` | Cloud API access token |
| `WHATSAPP_PHONE_NUMBER_ID` | Phone number ID to send from |
| `WHATSAPP_VERIFY_TOKEN` | Any secret string, also entered in the Meta webhook settings |
| `WHATSAPP_APP_SECRET` | App secret; when set, webhook signatures are verified (recommended) |
| `WHATSAPP_PUBLIC_NUMBER` | Your WhatsApp number, digits only (e.g. `919812345678`), for the shareable `wa.me` booking link |
| `WHATSAPP_REMINDER_TEMPLATE` | Name of an approved template for reminders (see below) |
| `WHATSAPP_STAFF_ALERT_TEMPLATE` | Approved template for alerting a stylist outside the 24h window |
| `WHATSAPP_TEMPLATE_LANGUAGE` | Template language code, default `en` |

### Reminders and the 24-hour window

WhatsApp only delivers free-form messages within 24 hours of the client's last
message. Reminders usually fall outside that window, so create a **Utility** message
template in WhatsApp Manager with three body variables, for example:

> Hi {{1}}, this is a reminder of your {{2}} appointment on {{3}}. Reply *menu* to manage it.

Set `WHATSAPP_REMINDER_TEMPLATE` to its name, and run reminders from cron:

```cron
*/15 * * * * cd /path/to/salon && .venv/bin/python manage.py send_reminders
```

Each appointment gets one reminder when it is within `SALON_REMINDER_LEAD_HOURS` (default 24 hours).

## Importing past invoices

*Import* (manager login) takes a CSV with **one row per service** on each invoice:

```csv
invoice,date,phone,name,service,price,quantity,stylist
INV-1001,2026-05-10,919812345678,Asha,Haircut,250,1,Priya
INV-1001,2026-05-10,919812345678,Asha,Hair colour,1300,1,Priya
```

Required: `date`, `phone`, `service`, `price`. Common alternatives (`Mobile`, `Bill No`, `Amount`, `Staff`, …) and
date formats (`2026-05-10`, `10/05/2026`, `10-May-2026`) are recognised. Rows sharing an invoice number
become one invoice, and re-importing a file skips invoices already imported. Services not in the catalogue are
kept as text; add a service with the same name later and past lines link to it. See `sample_invoices.csv`.

Scanned or PDF invoices can be attached per client (*+ Past invoice*). They are stored under `SALON_MEDIA_ROOT`
and only served to logged-in staff who can see that client.

## Stylists on their own WhatsApp

1. Edit the stylist: add their WhatsApp number and tick **Forward chats to my WhatsApp**.
2. Ask them to save the salon number and send it any message (e.g. `#help`). This opens WhatsApp's 24-hour window.
3. When a client chats, the stylist gets `💬 Asha (+91…): message`. They **swipe-reply** to answer that client. A plain
   message goes to the client they were last talking to.

WhatsApp only delivers free-form messages to a stylist who has messaged the salon number in the last 24 hours.
To reach them outside that window, create a Utility template with two variables (client name, message preview),
for example *"{{1}} is waiting for you on WhatsApp: {{2}}. Reply here to answer."*, and set
`WHATSAPP_STAFF_ALERT_TEMPLATE` to its name.

A stylist's number that has forwarding turned on is treated as staff, not as a client. They should book their own
appointments through the front desk.

## Other settings

| Variable | Default |
| --- | --- |
| `SALON_NAME` | `Glow Salon` |
| `SALON_ADDRESS` | *(empty)* |
| `SALON_CURRENCY` | `₹` |
| `SALON_TIME_ZONE` | `Asia/Kolkata` |
| `SALON_SLOT_INTERVAL_MINUTES` | `30` |
| `SALON_BOOKING_WINDOW_DAYS` | `14` |
| `SALON_REMINDER_LEAD_HOURS` | `24` |
| `DJANGO_SECRET_KEY` | dev key. **Set this in production** |
| `DJANGO_DEBUG` | `true`. Set to `false` in production |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | e.g. `https://your-domain` |
| `DATABASE_URL` | *(unset: local SQLite)*, e.g. `postgres://user:pass@host/db` |
| `SALON_DB_PATH` | `salon.sqlite3` (when `DATABASE_URL` is unset) |
| `DJANGO_ADMIN_USERNAME` / `DJANGO_ADMIN_PASSWORD` | Manager login created or updated by `build.sh` |
| `SALON_SEED_DEMO` | `false`. `true` loads sample services, stylists and hours on deploy |
| `SALON_MEDIA_ROOT` | `media/` (uploaded invoice scans) |

## Layout

```
salon/
  booking/     models, slot availability, dashboard views
    insights.py         price history, charge prompts, due / cross-sell / upsell suggestions
    invoice_import.py   CSV import of past invoices
  whatsapp/    Cloud API client, webhook, booking bot, notification messages, inbox models
    routing.py          anchor routing, relay to stylists' own WhatsApp, staff visibility
  templates/   dashboard HTML
  static/      stylesheet
```
