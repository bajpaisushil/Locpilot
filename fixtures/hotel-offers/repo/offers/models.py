"""Domain models for hotel room offers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Offer:
    hotel_id: str
    room_type: str
    rate_plan: str
    supplier: str
    price: float
    currency: str = "EUR"

    def dedupe_key(self) -> tuple[str, str, str]:
        """Two offers for the same hotel, room type and rate plan are the same product."""
        return (self.hotel_id, self.room_type, self.rate_plan)
