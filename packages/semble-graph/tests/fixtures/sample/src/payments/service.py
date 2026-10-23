from .gateway import Gateway
from .models import Payment


class PaymentService:
    """Coordinates payments."""

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    def charge(self, payment: Payment) -> bool:
        return self.gateway.authorize(payment)

    def retry(self, payment: Payment) -> bool:
        return self.charge(payment)


def create_service(gateway: Gateway) -> PaymentService:
    return PaymentService(gateway)
