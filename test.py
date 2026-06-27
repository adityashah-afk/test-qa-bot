def calculate_discount(price, discount_percent):
    discount_amount = price * (discount_percent / 100)
    final_price = price - discount_amount
    return final_price
    def divide(a, b):
    return a / b   # <-- BUG: This will crash if b is 0!
