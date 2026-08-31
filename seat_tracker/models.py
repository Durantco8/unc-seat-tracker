from dataclasses import dataclass


@dataclass
class SectionStatus:
    """A snapshot of one class section's status from a single scrape."""

    term: str
    subject: str
    catalog_number: str
    class_section: str
    class_number: str
    description: str
    available_seats: int
    instruction_type: str
    schedule: str
