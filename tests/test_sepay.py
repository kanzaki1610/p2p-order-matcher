from app.sepay import combined_description, normalize_bank, parse_sepay_datetime, transaction_id
from app.schemas import SePayWebhookIn


def sample_payload() -> SePayWebhookIn:
    return SePayWebhookIn(
        id=12345,
        gateway="MBBank",
        transactionDate="2026-09-20 16:06:00",
        accountNumber="0000000000",
        content="TESTP2P001 FT26264040336009",
        transferType="in",
        transferAmount=10000,
        accumulated=0,
        referenceCode="FT26264367805804",
        description="Bank transfer",
    )


def test_sepay_adapter():
    payload = sample_payload()
    assert normalize_bank(payload.gateway) == "MB"
    assert transaction_id(payload) == "FT26264367805804"
    assert "TESTP2P001" in combined_description(payload)
    assert parse_sepay_datetime(payload.transactionDate).tzinfo is not None


def test_sepay_normalizes_vpbank_and_acb_aliases():
    assert normalize_bank("VPB") == "VPBANK"
    assert normalize_bank("Vietnam Prosperity Bank") == "VPBANK"
    assert normalize_bank("ACB") == "ACB"
    assert normalize_bank("Asia Commercial Bank") == "ACB"
