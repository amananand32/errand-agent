"""Seed data: 50+ stored memories (mixed provenance and status) and a long prior
conversation, used by the context-management evals and the demo."""
from __future__ import annotations

from .memory import MemoryStore
from .store import Store

IRRELEVANT = [
    ("food.cuisine", "Loves South Indian breakfast"), ("food.allergy_note", "No peanuts in snacks"),
    ("family.kids", "Two kids, 7 and 10"), ("family.parents_visit", "Parents visit every Diwali"),
    ("school.pickup", "Kids' school pickup at 15:30"), ("hobby.reading", "Reads thrillers"),
    ("hobby.music", "Carnatic music on weekends"), ("travel.next_trip", "Goa in late December"),
    ("travel.frequent", "Travels to Pune monthly for work"), ("car.model", "Drives a Nexon EV"),
    ("car.service_center", "Tata service at Domlur"), ("work.company", "Works in fintech"),
    ("work.hours", "WFH Mon/Fri"), ("birthday.month", "March"), ("anniversary.month", "August"),
    ("gift.style", "Prefers experience gifts"), ("plants.balcony", "Has 12 balcony plants"),
    ("fitness.gym", "Gym at 6:30 am"), ("tv.shows", "Watches cricket"), ("coffee.order", "Filter coffee"),
    ("news.pref", "Reads business news"), ("cricket.team", "Supports RCB"), ("phone.os", "Android"),
    ("bank.name", "HDFC"), ("insurance.renewal", "Home insurance renews in June"),
    ("library.card", "Member at Atta Galatta"), ("charity.pref", "Donates to Akshaya Patra"),
    ("restaurant.fav", "MTR for weekend breakfast"), ("grocery.app", "Uses BigBasket"),
    ("laundry.day", "Laundry on Sundays"), ("milk.delivery", "Milk at 6 am"),
    ("newspaper", "The Hindu"), ("tailor", "Local tailor near 12th Main"), ("doctor.family", "Dr. Rao, Sundays"),
    ("vet.clinic", "Cessna Lifeline for the dog"), ("electricity.provider", "BESCOM"),
    ("water.supply", "Tanker water on Thursdays"), ("guests.weekend", "Guests most weekends"),
    ("delivery.instructions", "Leave parcels with security"),
]


def seed_memories(store: Store, user_id: str, ts: str = "2026-10-01T10:00:00+05:30",
                  include_area: bool = True) -> dict[str, int]:
    mem = MemoryStore(store)
    ids: dict[str, int] = {}
    if include_area:
        ids["old_area"] = mem.add(user_id, "area.preferred", "Koramangala", "explicit", "2026-03-01T10:00:00+05:30",
                                  evidence="I live in Koramangala").id
        ids["area"] = mem.add(user_id, "area.preferred", "Indiranagar", "explicit", ts,
                              evidence="We moved to Indiranagar last month").id
    ids["earliest"] = mem.add(user_id, "time.earliest", "09:00", "explicit", ts,
                              evidence="never schedule anything before 9 am").id
    ids["language"] = mem.add(user_id, "language", "English", "explicit", ts, evidence="English is fine").id
    ids["tech_ele"] = mem.add(user_id, "technician.electrical", "PRV-ELE-02", "explicit", ts,
                              evidence="Ramesh from Sharma Electricals is great, use him again").id
    ids["budget_cln"] = mem.add(user_id, "budget.cleaning", "2000", "explicit", ts,
                                evidence="keep cleaning under 2000").id
    ids["pets"] = mem.add(user_id, "home.pets", "Friendly dog named Bruno; keep the gate closed", "explicit", ts,
                          evidence="We have a friendly dog, Bruno - please ask them to keep the gate closed").id
    ids["parking"] = mem.add(user_id, "home.parking", "Visitor parking at basement B1", "explicit", ts,
                             evidence="visitors can park at B1").id
    ids["ac_inferred_confirmed"] = mem.add(user_id, "technician.ac_service", "PRV-AC-02", "inferred", ts,
                                           confirmed=True, evidence="(inferred from 3 AC bookings)").id
    # inferred, NOT confirmed -> must never reach the model
    ids["latest_inferred"] = mem.add(user_id, "time.latest", "18:00", "inferred", ts,
                                     evidence="(inferred: all past bookings ended before 6pm)").id
    ids["plb_inferred"] = mem.add(user_id, "technician.plumbing", "PRV-PLB-02", "inferred", ts,
                                  evidence="(inferred: last plumber)").id
    ids["budget_plb_inferred"] = mem.add(user_id, "budget.plumbing", "900", "inferred", ts,
                                         evidence="(inferred: typical spend)").id
    for i, (k, v) in enumerate(IRRELEVANT):
        mem.add(user_id, k, v, "explicit", ts, evidence=v)
    # forgotten earlier -> tombstone, value gone
    mem.add(user_id, "home.address", "Flat 4B, Lotus Apartments, 12th Main", "explicit", ts,
            evidence="my address is Flat 4B, Lotus Apartments, 12th Main")
    mem.forget(user_id, "home.address", ts)
    ids["forgotten_address"] = store.one("SELECT id FROM memories WHERE key='home.address'")["id"]
    return ids


def seed_history(store: Store, user_id: str, n_exchanges: int = 160, session: str = "old") -> int:
    topics = [
        ("Can you get someone to fix the bathroom fan?", "Booked BrightSpark Electricals for Tuesday 10 am."),
        ("What did the AC servicing cost last time?", "It was ₹1,000 with Arctic Air Services."),
        ("Remind me which plumber came in September?", "Kumar Pipe Works, Anil Kumar, on 3 Sep."),
        ("Is there anyone good for termite treatment?", "TermiteGuard has a 4.9 rating but costs ₹2,500."),
        ("Thanks! Also, my kids have exams next week so keep evenings quiet.", "Noted, I'll avoid evenings."),
        ("Book a deep clean before Diwali", "DeepClean Co is available Saturday 9 am for ₹2,200."),
        ("Never mind, cancel that", "Okay, nothing was booked."),
        ("Do you know a good carpenter for a wardrobe door?", "WoodWise Carpentry, ★4.5, from ₹800."),
    ]
    n = 0
    for i in range(n_exchanges):
        u, a = topics[i % len(topics)]
        day = 1 + (i // 6) % 28
        ts = f"2026-09-{day:02d}T10:{i % 60:02d}:00+05:30"
        store.add_message(session, user_id, "user", u + ("" if i % 3 else f" (ref {i})"), ts)
        store.add_message(session, user_id, "assistant", a, ts)
        n += 2
    # the original statements behind memories that will be forgotten/corrected in tests
    store.add_message(session, user_id, "user",
                      "We have a friendly dog, Bruno - please ask them to keep the gate closed", "2026-09-30T09:00:00+05:30")
    store.add_message(session, user_id, "assistant", "Got it, I'll mention Bruno to technicians.", "2026-09-30T09:00:00+05:30")
    return n + 2
