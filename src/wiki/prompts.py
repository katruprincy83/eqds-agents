"""Per-page research questions the wiki builder sends to the moderator (design §10: one run per page, caller='wiki')."""
PAGE_QUESTIONS = {
    "Company": "Give a company overview of {name}: what it does, scale, recent financial and business developments, valuation context and how GIC is positioned.",
    "Industry": "Describe the industry backdrop of {name}: demand and supply drivers, competitors and where {name} sits, with evidence.",
    "Catalysts": "What are the upcoming and recent catalysts for {name} (earnings, regulation, orders, news) and what could change the picture in the next month?",
    "Debate": "Where do the external managers, GIC trading, quant, brokers, experts and news agree and disagree about {name}? Highlight the open points.",
    "Internal View": "What is GIC's own view of {name}: analyst ratings and active weight history and rationale, internal notes, and our trading in the last 30 days?",
    "External View": "What do external managers, brokers (ratings, targets, valuation) and expert calls say about {name}?",
    "ESG": "What do the available sources say about ESG, governance and regulatory risks for {name}? State clearly where evidence is missing.",
}
