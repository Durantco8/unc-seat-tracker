# UNC Seat Tracker

A tool that watches UNC course sections for open seats and emails you when one opens up, so you don't have to keep refreshing the registration page during add/drop.

## Why

Every registration cycle, students end up manually refreshing the class search page hoping a seat opens in a full section. This automates that: you tell it which sections you care about, and it checks periodically and notifies you the moment a seat becomes available.

## How it works

1. A scraper checks UNC's public class search for the sections you're watching and pulls current seat counts
2. A polling loop runs on an interval, checking every watched course (deduplicated so watching multiple sections of the same course doesn't mean multiple requests)
3. When a section goes from full to open, a notification is queued and emailed to you, with retries if delivery fails
4. A small web dashboard lets you add/remove watches and see notification history

## Stack

- **Backend:** Python, Flask
- **Database:** SQLite + SQLAlchemy Core
- **Scraping:** `requests` + BeautifulSoup
- **Frontend:** plain HTML/JS (no framework — the UI is one page with a form and a list, so React would've added more overhead than value here)
- **Notifications:** SMTP email with exponential backoff retries

## Running it locally

```bash
git clone <this repo>
cd unc-seat-tracker
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the API + dashboard:
```bash
flask --app seat_tracker.api run --port 5001
```
Then open `http://localhost:5001`.

Run the poller (checks watched sections and sends notifications):
```bash
export SMTP_USER="your-email@gmail.com"
export SMTP_PASSWORD="your-app-password"
python -m seat_tracker
```

(SMTP is optional — without it, watches and the API still work, you just won't get real emails.)

## Testing

```bash
python -m pytest tests/
```

Includes a full integration test that exercises the scrape → detect → notify pipeline end to end using injected fakes, so it runs without hitting the network or sending real email.

## Notes

This only uses UNC's public class search (`reports.unc.edu/class-search`), which doesn't require login and is separate from ConnectCarolina. It's not affiliated with or endorsed by UNC. Polling is intentionally rate-limited and identifies itself with a descriptive User-Agent to avoid putting load on the source site.

See `DECISIONS.md` for the reasoning behind the bigger design choices in this project.
