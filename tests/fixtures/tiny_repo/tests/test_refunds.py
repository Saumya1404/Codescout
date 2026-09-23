from shop.refunds import validate_refund


def test_partial_refund_is_valid() -> None:
    assert validate_refund(10, 20)
