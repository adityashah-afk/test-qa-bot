
# Fixed version that handles edge cases.
def calculate_discount(price, discount_percent):
    """Calculate the final price after discount."""
    if price < 0:
        return 0.0
    if discount_percent < 0:
        discount_percent = 0.0
    if discount_percent > 100:
        discount_percent = 100.0
    discount_amount = price * (discount_percent / 100)
    final_price = price - discount_amount
    return final_price

def apply_sale_to_cart(cart_items, discount_percent):
    total = 0
    for item in cart_items:
        total += calculate_discount(item['price'], discount_percent)
    return total
