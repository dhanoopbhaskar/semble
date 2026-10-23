class Payment:
    """A payment request."""

    def __init__(self, customer: str, amount: int) -> None:
        self.customer = customer
        self.amount = amount

    def total(self) -> int:
        return self.amount
