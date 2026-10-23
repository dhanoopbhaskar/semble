from src.payments.gateway import Gateway
from src.payments.models import Payment
from src.payments.service import PaymentService


def test_charge() -> None:
    service = PaymentService(Gateway())
    assert service.charge(Payment("alice", 10))
