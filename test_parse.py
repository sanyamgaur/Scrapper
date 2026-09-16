"""Sanity check the extractor against a Blinkit-shaped snippet tree."""
import json
from blinkit_parse import extract_products, find_pagination

PAYLOAD = {
  "response": {
    "snippets": [
      {"widget_type": "banner", "data": {"id": "b1", "image": {"url": "x.png"}}},
      {"widget_type": "product_card_snippet_type_2", "data": {
          "identity": {"id": 481234},
          "merchant_id": "31415",
          "name": {"text": "Amul Taaza Toned Milk"},
          "brand_name": {"text": "Amul"},
          "variant": {"text": "500 ml"},
          "normal_price": {"text": "₹28"},
          "mrp": {"text": "₹30"},
          "image": {"url": "https://cdn.grofers.com/milk.jpg"},
          "inventory": 42,
          "atc_action": {"add_to_cart": {"cart_item": {"product_id": 481234}}}
      }},
      {"widget_type": "product_card_snippet_type_2", "data": {
          "identity": {"id": 99881},
          "name": {"text": "Lay's Classic Salted"},
          "unit": "52 g",
          "price": 20,
          "mrp": 20,
          "out_of_stock": True
      }}
    ],
    "pagination": {"has_more": True, "offset": 20},
    "postback_params": "cursor_abc123"
  }
}

rows = extract_products(PAYLOAD, {"l0_cat": 14, "l1_cat": 922, "category_name": "Dairy", "source": "category"})
for r in rows:
    r.pop("raw")
    print(json.dumps(r, ensure_ascii=False))
print("pagination:", find_pagination(PAYLOAD))
assert len(rows) == 2, "expected 2 products, got %d" % len(rows)
assert rows[0]["price"] == 28.0 and rows[0]["mrp"] == 30.0
assert rows[0]["discount_pct"] == 6.67
assert rows[0]["brand"] == "Amul" and rows[0]["unit"] == "500 ml"
assert rows[0]["in_stock"] is True and rows[1]["in_stock"] is False
assert rows[0]["l0_cat"] == 14
print("\nALL ASSERTIONS PASSED")
