"""Cross-border commerce engines: India (Blinkit) -> US storefront.

Pipeline:
    ingest   -> normalize scraped catalogue into crossborder.db
    classify -> compliance/shippability verdict per SKU (rules, then LLM tail)
    quote    -> shipping cost estimate for a cart
    price    -> landed cost + USD list price
    stock    -> live availability guard before an order is accepted
"""

__version__ = "0.1.0"
