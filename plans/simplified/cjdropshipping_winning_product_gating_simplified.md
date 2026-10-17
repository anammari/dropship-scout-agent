## The CJ "winning product" gate in plain English

**The problem it solves:** CJdropshipping used to be read for stock and price
only. If a listing was in stock and active, it went to the AI for judgement —
even if nobody had ever bought it. That let unproven listings through.

**The fix:** before a CJ search result is allowed to cost a single further
lookup, it must prove it sells. This mirrors what the AliExpress side used to
do (that side is retired now).

---

**Phase 1 — What can we actually measure?**
We probed CJ's own MCP tools live to find out which numbers its listings really
carry.

- **What we found:** CJ publishes how many dropshippers have imported a listing
  (`listedNum`) — that number is real and reliable.
- **What we did NOT find:** any sales figure at all. No "sold", no "orders", no
  "sales count", under any name, on any of CJ's 62 tools. The order tools report
  *our own* orders, not the market's.
- **A trap avoided:** `variantVolume` looks like a sales number but is the
  parcel's size in cubic millimetres.
- Freight, however, is genuinely quotable — CJ returns real shipping prices to
  Australia.

**Phase 2 — One number to judge by.**
So the gate uses `listedNum` alone, with a floor of **150** (below that, a
listing is treated as unproven). Nothing anywhere looks for a sales figure —
enforcing a number CJ never publishes would have thrown away every product.

**Phase 3 — Where the gate runs.**
On the raw search results, *before* any detailed product lookup. A weak listing
therefore costs nothing: no detail call, no freight quote. A listing that
doesn't report its count at all is also dropped — "we couldn't tell" is not a
pass.

**Phase 4 — Strongest first.**
Survivors are sorted by listing count, highest first, so the most widely-sold
listings are looked at first, and the run's log shows that order.

**Phase 5 — Tests.**
Each case is covered: passing at the floor, dropped below it, dropped when the
count is missing, the exact log lines, and that no detailed lookup happens for a
listing that failed the gate.

**Phase 6 — Real shipping costs.**
Shipping was being counted as **zero**, which made the landed cost look a
quarter of what it really is — one exported product looked profitable at
AUD 5.19 when the shipping quote alone was AUD 19.42. Now every listing carries
a **real quoted freight figure** for a named destination (Australia at launch),
either by naming the cheapest delivery option or by pinning one you choose. A
listing whose freight cannot be quoted is dropped, because "free shipping" was
never true.

**Phase 7 — No invented shipping promises.**
The customer-facing shipping line used to be written by the AI, which once
claimed "Free standard shipping on this item" on a product that cost
USD 9.37–12.53 to post. It is now **built by code from the supplier's own
quote** — the service and the delivery window, nothing more — and the AI is
forbidden from writing it at all.

---

**Why this matters for The Morning Anthem:** every product reaching the gold
list has a proven seller, a real landed cost including freight, and a shipping
line you can defend to a customer.
