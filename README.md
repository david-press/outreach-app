# Outreach App

Finds businesses of any type in any city, researches them, drafts personalized
email/WhatsApp messages, and tracks your pipeline. You review and send; the app logs it.

## Setup (once)
1. Install Python 3.10+ (tick "Add to PATH" on Windows).
2. Free Groq key: https://console.groq.com/keys -> paste into `.env` (GROQ_API_KEY=...).
3. `pip install -r requirements.txt`

## Run
`streamlit run app.py` (from inside this folder), or double-click `run.bat`.

## Daily use
1. Sidebar: type a city + country (e.g. `Lagos, Nigeria`, `Paris, France`, `Doha, Qatar`),
   a business type (e.g. `restaurants`), how many -> **Find & draft messages**.
2. **Send** tab: edit the text, tap Email/WhatsApp, send, tap **Done, I sent it**.
3. **Follow-ups** tab: due on day 3 and day 8.
4. **Pipeline** tab: set replied / call / won / lost. **Stats** tab: reply rate.

## Business types
restaurants, accountants, hotels, pharmacies, clinics, salons, supermarkets, retail,
electronics, schools, law, real estate, gyms, auto, logistics, construction, insurance,
travel agencies, bakeries, hardware. Anything else: use a raw OpenStreetMap tag,
e.g. `tag:amenity=bar|pub` (list: wiki.openstreetmap.org/wiki/Map_features).

## Offers per business type
Each type has its own offer. Use **Edit my offer** in the sidebar; it saves to `offers/`.
Write the offer BEFORE drafting for a new type, or it falls back to a generic one.

## Countries, phones, languages
Country is detected from the city. Local phone numbers (e.g. 0803...) are converted to
international format for WhatsApp links automatically. Messages are drafted in French,
German, Spanish, Portuguese or Italian for those countries, English otherwise
(or the website's language when clear).

## Honest limits
- Data comes from OpenStreetMap. In many African cities, fewer businesses have a phone or
  website listed than in the US/UK/Europe. If a city returns few results, try other cities
  or types, or paste your own list into `data/prospects.csv` (columns: firm_name, website,
  contact_name, contact_role, email, phone, country, address, segment, info).
- Businesses with no website get shorter, less personalized messages.
- Firms with no email and no phone are skipped automatically.
- WhatsApp: keep cold sends under ~10/day from your business number; email is safer.

## Data
Saved in `data/outreach.csv` (open in Excel/Sheets). Back it up.

## Deploying on Streamlit Community Cloud (use it from your phone)

Streamlit Cloud erases the app's files on every restart, and it has no `.env` file. So:

1. **Never push `.env`.** It is git-ignored. If you ever pushed it, delete it from the repo
   and create NEW keys at Groq / serper.dev / serpapi.com (old ones are exposed).
2. **Permanent storage (required).** Create a second, PRIVATE GitHub repo, e.g. `outreach-data`
   (tick "Add a README" so it has a `main` branch). Then GitHub > Settings > Developer settings >
   Personal access tokens > Fine-grained tokens: access to ONLY that repo, permission
   "Contents: Read and write". Copy the token.
3. **Secrets.** App > Settings > Secrets: paste `.streamlit/secrets.toml.example` filled with your values
   (Groq key, map key, `DATA_GITHUB_TOKEN`, `DATA_GITHUB_REPO`, `APP_PASSWORD`).
4. **Private app.** App > Settings > Sharing: restrict viewing to your own email (and keep APP_PASSWORD set).
5. Reboot the app, click **Test my keys**: it must show `Storage: OK (GitHub repo ...)`.

Use ONE copy of the app at a time (cloud OR laptop), since they would not share data.
Your day, daily limit and follow-up dates follow `APP_TZ` (default Africa/Lagos).
