def validate_refund(amount: int, paid: int) -> bool:
    return 0 < amount <= paid
