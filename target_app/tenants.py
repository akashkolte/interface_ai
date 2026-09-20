"""Tenant configuration for the target app.

The premise from the brief: hundreds of institutions run the *same underlying
vendor product*, "configured, branded, and versioned differently". So this is one
codebase driven by per-tenant config, not two forked apps. The differences below
are exactly the kinds that break naive automation: relabelled fields, reordered
navigation, an extra interstitial, a different product version.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    display_name: str
    app_version: str
    # 5010/5011 rather than 5000/5001: macOS Control Center (AirPlay
    # Receiver) listens on 5000 and silently 403s every request.
    port: int

    # Field and control labels. Automation that hardcodes "Member ID" breaks here.
    label_member_id: str
    label_search_button: str
    label_savings_balance: str
    label_open_subaccount: str
    label_confirm_button: str
    label_account_nickname: str
    label_initial_deposit: str

    # Structural differences.
    nav_order: tuple[str, ...]
    # creditunion_b interposes a "terms acknowledgement" page that the base
    # tenant does not have. A replay must tolerate this without a re-record.
    has_terms_interstitial: bool = False

    theme: dict[str, str] = field(default_factory=dict)


BASE = TenantConfig(
    tenant_id="base",
    display_name="Northgate Federal Credit Union",
    app_version="CoreServicing 7.2",
    port=5010,
    label_member_id="Member ID",
    label_search_button="Search",
    label_savings_balance="Savings Balance",
    label_open_subaccount="Open Sub-Account",
    label_confirm_button="Confirm",
    label_account_nickname="Account Nickname",
    label_initial_deposit="Initial Deposit",
    nav_order=("Member Search", "Accounts", "Transactions", "Admin"),
    has_terms_interstitial=False,
    theme={"bar": "#1f3a63", "accent": "#c8d4e8"},
)

CREDITUNION_B = TenantConfig(
    tenant_id="creditunion_b",
    display_name="Riverbend Community CU",
    app_version="CoreServicing 7.4",
    port=5011,
    # Same product, different configured vocabulary.
    label_member_id="Account Holder #",
    label_search_button="Find Member",
    label_savings_balance="Share Savings Balance",
    label_open_subaccount="Add Sub-Account",
    label_confirm_button="Submit Request",
    label_account_nickname="Nickname",
    label_initial_deposit="Opening Deposit",
    nav_order=("Accounts", "Member Search", "Admin", "Transactions"),
    has_terms_interstitial=True,
    theme={"bar": "#5a1f2e", "accent": "#e8cdd4"},
)

TENANTS: dict[str, TenantConfig] = {
    BASE.tenant_id: BASE,
    CREDITUNION_B.tenant_id: CREDITUNION_B,
}
