# SEO Agent (Telegram-driven, free-tier tools)

Telegram par website bhejo (`/newsite <url>`) -> agent **poori site crawl karke
agency-style audit PDF** banata hai (screenshots, charts, scores) — koi bhi
content ya changes tab tak nahi hote. Audit review karne ke baad **"Scratch
Start"** bolo tab agent kaam shuru karta hai [is phase ka code abhi is build
mein nahi hai — audit tak ban chuka hai, aage ka phase alag se aayega].

Aage ka planned flow: existing pages ko polish/rewrite karna (naye pages khud
se nahi banayega), on-page optimization, Search Console resubmission,
technical/local/off-page SEO, aur "sent"/"go ahead" review loop se developer
ke saath sync rehna, aakhir mein ongoing rank monitoring jab tak "/stop" na bolo.

## Kaunse tools/keys chahiye (sab free)

| Cheez | Kahan se milegi | Time |
|---|---|---|
| Telegram Bot Token | [@BotFather](https://t.me/BotFather) par `/newbot` | 2 min |
| Apna Telegram User ID | [@userinfobot](https://t.me/userinfobot) ko message karo | 1 min |
| Gemini API key (content likhne ke liye) | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | 2 min |
| PageSpeed Insights API key (site speed audit) | [Google Cloud Console](https://console.cloud.google.com/apis/library/pagespeedonline.googleapis.com) -> Enable API -> Credentials -> Create API key | 5 min |
| Search Console access (rank tracking) | Google Cloud Console -> Service Account bana kar JSON key download karo, phir uska email Search Console property mein "Full user" add karo | 10 min |
| Open PageRank API key (authority score) | [openpagerank.keywordseverywhere.com/dashboard](https://openpagerank.keywordseverywhere.com/dashboard) -> free Keywords Everywhere account bana kar sign in -> dashboard se OpenPageRank key generate karo | 3 min |

Koi bhi in mein se paid nahi hai. Gemini aur PageSpeed dono free-tier quota ke
saath free hain (bohot zyada daily requests karoge tabhi limit lagegi).

### Google Search Console service account setup (detail)

1. [console.cloud.google.com](https://console.cloud.google.com) par ek project banao
2. "Search Console API" enable karo (APIs & Services -> Library)
3. IAM & Admin -> Service Accounts -> Create -> koi bhi naam do -> JSON key download karo
4. Us JSON file ko `credentials/gsc_service_account.json` mein save karo
5. Downloaded JSON mein `client_email` field milega (kuch aisa: `xyz@project.iam.gserviceaccount.com`)
6. Search Console ([search.google.com/search-console](https://search.google.com/search-console)) mein apni verified property kholo -> Settings -> Users and permissions -> Add user -> wahi client_email daalo, permission "Full"

## Install (local dev)

```bash
pip install -r requirements.txt
```

`.env.example` ko `.env` mein copy karo aur saari keys bhar do.

```bash
python bot.py
```

**Note:** India mein kai ISPs (Jio/Airtel etc.) `api.telegram.org` ko
block/reset kar dete hain, chahe baaki internet chal raha ho. Agar bot local
machine par start hone ke baad Telegram se connect na ho, VPN try karo ya
seedha cloud deployment (neeche) par jao — wahan se ye issue nahi aata.

## Cloud deployment (Render, free, recommended)

Render ka free web service 24/7 available hai, VPN ki zaroorat nahi, aur
Telegram **webhook mode** use karta hai (polling ke bajaye — isliye
`WEBHOOK_URL` env var set karna padta hai).

Render ka free tier har restart/redeploy par disk wipe kar deta hai, isliye
site ka progress (kaunsa stage, published articles) **Upstash Redis** (free
forever) mein persist hota hai — local file storage automatically fallback
ban jaata hai agar Upstash configure nahi hai.

### 1. Upstash Redis (free, persistent state)

1. [console.upstash.com](https://console.upstash.com) par sign up karo (GitHub se ho sakta hai)
2. "Create Database" -> koi bhi region choose karo -> Free plan
3. Database open karo, "REST API" section se `UPSTASH_REDIS_REST_URL` aur `UPSTASH_REDIS_REST_TOKEN` copy kar lo

### 2. Code ko GitHub par push karo

Render GitHub repo se deploy karta hai.

```bash
git init
git add .
git commit -m "Initial SEO agent"
```

Phir [github.com/new](https://github.com/new) par ek (private) repo banao, aur:

```bash
git remote add origin <your-repo-url>
git branch -M main
git push -u origin main
```

### 3. Render par deploy karo

1. [render.com](https://render.com) par GitHub se sign up karo
2. "New +" -> "Web Service" -> apna GitHub repo select karo (`render.yaml` auto-detect ho jayega)
3. Environment tab mein ye keys daalo (values apne `.env` se copy karo):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_ALLOWED_USER_ID` (apna Telegram user ID — [@userinfobot](https://t.me/userinfobot) se milega)
   - `GEMINI_API_KEY`
   - `PAGESPEED_API_KEY` (optional)
   - `UPSTASH_REDIS_REST_URL`
   - `UPSTASH_REDIS_REST_TOKEN`
   - `OPEN_PAGERANK_API_KEY` (optional, skip karne par authority score "not connected" dikhega)
4. Deploy karo. Render ek URL dega, jaisे `https://seo-agent-bot.onrender.com`
5. Ek aur env var add karo: `WEBHOOK_URL` = wahi URL (bina trailing slash ke), phir redeploy karo (auto-redeploy ho jayega env var change karne par)

Bas — ab bot 24/7 chalega, Telegram par message bhejo aur reply aana chahiye.

### Free tier caveat

Render free web service pehli request par "cold start" leta hai agar
15 min se idle tha (~30-60 sec ka delay) — data loss nahi hota (Telegram
webhook delivery retry karta hai), bas pehla reply thoda late aa sakta hai.

## Telegram commands

- `/newsite <url>` — poori site crawl + audit PDF (screenshots, charts, scores, broken links, page-by-page appendix)
- Audit PDF ke saath **buttons** milte hain (ya wahi text likh sakte ho): `Approve Audit`, `Request Revision`, `Scratch Start`, `Pause`
  - `Scratch Start` — existing pages (sabse zyada issues wale pehle) content-update ke liye queue ho jate hain, naye pages kabhi invent nahi karta
  - `Request Revision` — agla message jo bhejoge wo revision note ke roop mein save ho jayega
  - `Pause` / `resume` — site ka kaam temporarily rok/shuru karo (`/stop` permanent hai, ye nahi)
- `sent` — bolo jab doc developer ko de diya
- `go ahead` — bolo jab developer ne live update kar diya (agla step trigger karta hai, us specific page ka audit karta hai jo abhi update hui)
- `/status` — sab tracked sites ka current stage
- `/stop <url>` — kisi site par kaam rokna

## Free-tier limitations (transparent rehna zaroori hai)

- **"Authority Score"** Moz ki asli (paid, trademarked) Domain Authority nahi
  hai — Open PageRank ka free proxy hai, report mein clearly labelled hai.
- **Broken-link checking** aur **competitor classification** free-tier speed
  budget ke andar capped hain (external link checks ~50, competitors 5
  keyword searches tak) — bade sites ka partial-but-honest coverage milega,
  capped hone par report mein note dikhega.
- **Competitor classification** (Direct/SERP/Directory/Informational) ek
  heuristic hai (keyword-match frequency + known-directory list) — free
  tools se ye Google ke asli SERP-overlap jitna precise nahi ho sakta.
- **"Keyword rankings"** sirf tab real hain jab us specific site ka Search
  Console connect ho. Warna PDF mein "connect Search Console" note dikhega,
  fake number nahi.
- **Crawl** max 200 pages tak capped hai (Render free tier ke time/resource
  budget ke liye) — bade sites ka partial audit hoga, report mein note hoga.
- **Search Console "submission"** ka matlab sitemap resubmit + index-status
  check hai (real, supported APIs) — Google arbitrary pages ko force-index
  karne ka free API nahi deta, isliye wo claim nahi kiya jayega.
- **Keyword volume**: exact search volume ke liye paid tools (Ahrefs/SEMrush)
  chahiye. Yahan Google Trends + DuckDuckGo autocomplete (dono free) use ho
  rahe hain jo directionally sahi hain, exact numbers nahi.
- **Competitor/SERP data**: DuckDuckGo search results use ho rahe hain (free,
  no API key) kyunki Google SERP scraping ToS violate karta hai. Results
  Google rankings se thoda alag ho sakte hain.
- **Backlink building**: fully automate nahi ho sakta free tools se — agent
  sirf opportunities suggest karega, outreach manual rahega.
- **Ranking timeline**: koi bhi tool Google ranking guarantee nahi kar sakta.
  Naye/low-competition keywords 4-8 hafte mein movement dikhate hain,
  competitive keywords 3-6+ mahine lete hain. Naya domain ho to Google ka
  "trust build" period bhi lagta hai (~2-3 mahine).

## Project structure

```
bot.py                          Telegram entrypoint
config.py                       env/config loader
seo_agent/
  pipeline.py                    stage orchestration per website
  storage/state_store.py          per-site state (Upstash Redis in cloud, local JSON fallback)
  research/
    site_crawler.py                 full-site crawl (free Screaming Frog alternative)
    authority.py                     Open PageRank authority score proxy
    screenshots.py                   Microlink.io screenshot capture
    keywords.py                     Google Trends + DuckDuckGo autocomplete keyword research
    competitors.py                   DuckDuckGo based competitor discovery
    site_audit.py                     on-page + PageSpeed audit (single page)
  reporting/
    pdf_report.py                    agency-style audit PDF (reportlab + matplotlib)
  content/
    generator.py                     Gemini article writer
    docx_writer.py                    markdown -> Word doc
  tracking/
    search_console.py                 GSC rank/traffic API
data/                             generated docs + audit PDFs + state (gitignored)
```
