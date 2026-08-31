"""Scrape UNC's public class-search page for available seat counts."""

import requests
from bs4 import BeautifulSoup

from seat_tracker.models import SectionStatus

SEARCH_URL = "https://reports.unc.edu/class-search/"
USER_AGENT = "unc-seat-tracker/0.1 (student project; contact: croutondurant@gmail.com)"


def check_seats(
    term: str, subject: str, catalog_number: str
) -> list[SectionStatus]:
    """Return current seat data for every section matching the query.

    Performs a full GET → parse CSRF → POST → parse results flow against
    the UNC class-search page.  Raises on HTTP errors rather than retrying.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    # --- Step 1: GET the search page to obtain a session cookie + CSRF token ---
    get_resp = session.get(SEARCH_URL)
    get_resp.raise_for_status()

    soup = BeautifulSoup(get_resp.text, "lxml")
    csrf_input = soup.find("input", attrs={"name": "csrfmiddlewaretoken"})
    if csrf_input is None:
        raise ValueError("Could not find csrfmiddlewaretoken in the search page HTML")
    csrf_token = csrf_input["value"]

    # --- Step 2: POST the search form ---
    form_data = {
        "csrfmiddlewaretoken": csrf_token,
        "term": term,
        "subject": subject,
        "catalog_number": catalog_number,
    }
    post_resp = session.post(SEARCH_URL, data=form_data)
    post_resp.raise_for_status()

    # --- Step 3: Parse the results table ---
    return _parse_results(post_resp.text)


def _parse_results(html: str) -> list[SectionStatus]:
    """Extract section rows from the results HTML table."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        return []

    # Build a header-index map so we aren't hard-coding column positions.
    headers = [th.get_text(strip=True) for th in table.find_all("th")]
    num_cols = len(headers)
    col = {name: idx for idx, name in enumerate(headers)}

    required = [
        "Subject", "Catalog Number", "Class Section", "Class Number",
        "Description", "Available Seats",
    ]
    for name in required:
        if name not in col:
            raise ValueError(
                f"Expected column '{name}' not found in table headers: {headers}"
            )

    sections: list[SectionStatus] = []
    # Track the last-seen subject/catalog_number because continuation rows
    # for the same course omit the leading columns (Subject, Catalog Number,
    # and Same As), giving them fewer cells than the header row.
    last_subject = ""
    last_catalog = ""

    for row in table.find_all("tr")[1:]:  # skip header row
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if not cells:
            continue

        # Pad short rows: if a row has fewer cells than headers, it's a
        # continuation row missing the leading columns.  Prepend empty
        # strings so indices line up with the header map.
        if len(cells) < num_cols:
            cells = [""] * (num_cols - len(cells)) + cells

        subject = cells[col["Subject"]] or last_subject
        catalog_number = cells[col["Catalog Number"]] or last_catalog
        last_subject = subject
        last_catalog = catalog_number

        seats_text = cells[col["Available Seats"]]
        try:
            available_seats = int(seats_text)
        except ValueError:
            available_seats = 0

        sections.append(
            SectionStatus(
                term=cells[col["Term"]] if "Term" in col else "",
                subject=subject,
                catalog_number=catalog_number,
                class_section=cells[col["Class Section"]],
                class_number=cells[col["Class Number"]],
                description=cells[col["Description"]],
                available_seats=available_seats,
                instruction_type=cells[col["Instruction Type"]]
                if "Instruction Type" in col
                else "",
                schedule=cells[col["Schedule"]]
                if "Schedule" in col
                else "",
            )
        )

    return sections
