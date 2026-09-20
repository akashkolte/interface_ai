"""Fake member records.

Entirely synthetic. The brief forbids real credentials and real PII, and this
data set is also what the redaction tests run against -- the SSN-shaped and
account-number-shaped fields exist specifically so we can prove they never reach
an artifact or a log.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SubAccount:
    number: str
    nickname: str
    kind: str
    balance: str


@dataclass
class Member:
    member_id: str
    name: str
    status: str            # ACTIVE | CLOSED
    savings_balance: str
    checking_balance: str
    # Deliberately sensitive-looking. Must never appear in artifacts or logs.
    ssn: str
    primary_account_number: str
    sub_accounts: list[SubAccount] = field(default_factory=list)


MEMBERS: dict[str, Member] = {
    "12345": Member(
        member_id="12345",
        name="Dana Whitfield",
        status="ACTIVE",
        savings_balance="$4,182.55",
        checking_balance="$913.20",
        ssn="000-00-0001",
        primary_account_number="4000000000001234",
        sub_accounts=[SubAccount("SUB-0001", "Vacation Fund", "Savings", "$250.00")],
    ),
    "67890": Member(
        member_id="67890",
        name="Marcus Oyelaran",
        status="ACTIVE",
        savings_balance="$27,904.10",
        checking_balance="$2,145.88",
        ssn="000-00-0002",
        primary_account_number="4000000000005678",
        sub_accounts=[],
    ),
    "55555": Member(
        member_id="55555",
        name="Priya Raghunathan",
        status="CLOSED",
        savings_balance="$0.00",
        checking_balance="$0.00",
        ssn="000-00-0003",
        primary_account_number="4000000000009999",
        sub_accounts=[],
    ),
}


def find_member(member_id: str) -> Member | None:
    return MEMBERS.get((member_id or "").strip())


def next_subaccount_number(member: Member) -> str:
    return f"SUB-{len(member.sub_accounts) + 1:04d}"
