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


def test_kienlongbank_aliases_and_other_banks_remain_distinct():
    for name in ("KienLongBank", "KLB", "Kien Long Bank", "Kiên Long",
                 "Ngân hàng TMCP Kiên Long", "Kien Long Commercial Joint Stock Bank"):
        assert normalize_bank(name) == "KIENLONGBANK"
    assert normalize_bank("ACB") != normalize_bank("KLB")
    assert normalize_bank("MBBank") == "MB"
    assert normalize_bank("VIB") == "VIB"


def test_kienlongbank_payment_identity_requires_correct_bank():
    from decimal import Decimal
    from app.models import BankTransaction, P2POrder
    from app.matching import payment_identity
    order = P2POrder(side="SELL", order_code="KLB123456789", fiat_amount=Decimal("10000"),
                     counterparty_name="Nguyen Van An", expected_bank="KLB")
    tx = BankTransaction(direction="CREDIT", bank="KienLongBank", amount=Decimal("10000"),
                         description="KLB123456789", transaction_id="KLB-TEST-ONLY")
    assert payment_identity(order, tx)
    tx.bank = "ACB"
    assert not payment_identity(order, tx)


def test_vietinbank_aliases_do_not_match_vietcombank_or_other_banks():
    from decimal import Decimal
    from app.models import BankTransaction, P2POrder
    from app.matching import payment_identity
    aliases = ("VietinBank", "Vietin Bank", "ICB", "CTG",
               "Ngân hàng TMCP Công thương Việt Nam",
               "Vietnam Joint Stock Commercial Bank for Industry and Trade")
    order = P2POrder(side="SELL", order_code="VIETIN123456789", fiat_amount=Decimal("10000"),
                     counterparty_name="Nguyen Van An", expected_bank="ICB")
    tx = BankTransaction(direction="CREDIT", bank="VietinBank", amount=Decimal("10000"),
                         description="VIETIN123456789", transaction_id="VIETIN-TEST-ONLY")
    for bank in aliases:
        assert normalize_bank(bank) == "VIETINBANK"
        tx.bank = bank
        assert payment_identity(order, tx)
    for bank in ("Vietcombank", "VCB", "VIB", "MB", "ACB", "KLB"):
        tx.bank = bank
        assert normalize_bank(bank) != "VIETINBANK"
        assert not payment_identity(order, tx)


def test_bidv_full_bank_names_match_only_bidv():
    from decimal import Decimal
    from app.models import BankTransaction, P2POrder
    from app.matching import payment_identity
    aliases = ("BIDV", "Ngân hàng TMCP Đầu tư và Phát triển Việt Nam",
               "Ngân hàng Đầu tư và Phát triển Việt Nam",
               "Bank for Investment and Development of Vietnam",
               "Joint Stock Commercial Bank for Investment and Development of Vietnam")
    order = P2POrder(side="SELL", order_code="BIDV123456789", fiat_amount=Decimal("10000"),
                     counterparty_name="Nguyen Van An", expected_bank=aliases[1])
    tx = BankTransaction(direction="CREDIT", bank="BIDV", amount=Decimal("10000"),
                         description="BIDV123456789", transaction_id="BIDV-TEST-ONLY")
    for bank in aliases:
        assert normalize_bank(bank) == "BIDV"
        tx.bank = bank
        assert payment_identity(order, tx)
    for bank in ("VIETINBANK", "Vietcombank", "MB", "ACB", "KLB"):
        tx.bank = bank
        assert not payment_identity(order, tx)
