## The pipeline in plain English

**Goal:** find products to sell on The Morning Anthem and leave the best ones
ready for you to add to the store.

**Step 1 — What are people searching for?**
We ask HasData's Google Trends what Australian shoppers are searching for,
split by your 5 brand pillars (The Brew & Fuel, The Sunrise Move, The Mindful
Awakening, The Restful Reset, The Morning Table). It's free-tier credit, and
it's the only part of the pipeline that costs anything.

**Step 2 — Filter out junk searches, then pick the best ones.**
Two passes, both by Jev (a decision model, not a chatbot):

- *Which searches are real shoppers?* Some trend searches aren't shoppers —
  they're people looking up news, how-tos, or brand gossip. Jev sorts real
  "I want to buy this product" searches from the rest. Only shopping-intent
  searches survive. Anything naming a supermarket or hardware chain is dropped
  outright.
- *Which of the survivors are worth paying for?* The research gives us more
  search terms than we can afford to look up, so Jev scores each one on how
  good it is as a search term for finding the product on a supplier site, and
  we keep the best 30. The 30 are spread across all five pillars in proportion
  to what the research found — so one loud pillar can't take every slot, and no
  pillar is left out. The rest are kept as a reserve, so a re-run costs nothing.

**Step 3 — Find proven winners.**
For each of the 30 search terms, we look at Google Shopping and grab real
products people are actually buying (they have ratings and review counts ✓).
Another Jev check confirms each winner fits one of your 5 pillars. Products
that don't fit any pillar are tossed. Survivors = your **"gold list"** — the
proof of demand, about 55 products.

**Step 4 — Turn winners into supplier search terms.**
First we try the **exact product title** as the search term. Only if that comes
up empty do we let the AI craft a similar-sounding, tightly-specific
alternative. Never vague.

**Step 5 — Get the actual products.**
We search **only CJdropshipping** (no AliExpress anymore) using those terms.
Each real product must pass every gate to survive: proven seller ✓, actually in
stock ✓, real shipping cost quoted ✓, worth the margin (AUD 10+ profit or 2.5x
markup) ✓, has 3+ good photos ✓. Survivors get saved as product folders with
photos and pricing info. CJ is free to query, so we can keep searching until we
find each product — it's time that's spent here, not money.

**Step 6 — Rank them.**
Jev scores every saved product against your gold list — how close is it to the
proven winner, and is it a good deal? You end up with a ranked report:
**shortlist → review → skip**.

**Then it's yours:** you review the shortlist and do the manual steps of adding
them to the store.

**Budget guardrail:** one full launch run uses at most **400 HasData credits**
of your 1,000/month (100 for the trend research, 300 for the Google Shopping
lookups), and a quick test run is capped at **10**. The Jev passes cost a few
cents on your OpenRouter balance, not HasData credits. All of it is enforced by
the code, not just by hoping you remember.
