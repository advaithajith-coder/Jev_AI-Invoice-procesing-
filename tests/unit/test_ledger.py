from decimal import Decimal

from jev_invoice.domain.ledger import balance_of, withheld_after_credit

D = Decimal


def test_balance_subtracts_everything_that_settles_the_bill() -> None:
    balance = balance_of(
        total=D("1100.00"),
        amount_due=D("1000.00"),  # after a 100 deposit
        credits=[D("50.00")],
        withheld=D("25.00"),
        payments=[(D("400.00"), D("0")), (D("100.00"), D("20.00"))],
    )

    assert balance.billed == D("1000.00")
    assert balance.balance == D("405.00")
    assert balance.state == "part_paid"


def test_paid_in_full_and_never_negative() -> None:
    paid = balance_of(
        total=D("100.00"),
        amount_due=None,
        credits=[],
        withheld=None,
        payments=[(D("98.00"), D("2.00"))],
    )
    over = balance_of(
        total=D("100.00"), amount_due=None, credits=[D("150.00")], withheld=None, payments=[]
    )

    assert paid.state == "paid" and paid.balance == 0
    assert over.balance == 0


def test_a_credit_settles_a_short_pay_first() -> None:
    assert withheld_after_credit(D("100.00"), D("60.00")) == D("40.00")
    assert withheld_after_credit(D("100.00"), D("100.00")) is None
    assert withheld_after_credit(None, D("10.00")) is None
