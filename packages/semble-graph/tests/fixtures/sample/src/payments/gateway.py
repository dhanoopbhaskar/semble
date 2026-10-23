class Gateway:
    """Talks to the payment provider."""

    def authorize(self, payment: object) -> bool:
        return True
