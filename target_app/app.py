"""The target application: a deliberately legacy member-servicing surface.

Stands in for a core banking back-office screen. One codebase, driven by
per-tenant config, because that mirrors the real situation the brief describes:
many institutions running the same vendor product, configured differently.

The flow the capabilities are recorded against:
    search -> member detail -> open sub-account -> confirmation

Faults are injected per-request via ?fault=<name> so every error and exceptional
state in the evidence runs is reproducible.
"""

from __future__ import annotations

import time
import uuid

from flask import Flask, render_template, request, redirect, url_for

from target_app.data import MEMBERS, Member, SubAccount, find_member, next_subaccount_number
from target_app.faults import FAULT_COOKIE, Fault, active_fault
from target_app.tenants import TENANTS, TenantConfig

SLOW_LOAD_SECONDS = 6.0


def _nav_links(cfg: TenantConfig) -> dict[str, str]:
    return {
        "Member Search": "/search",
        "Accounts": "/search",
        "Transactions": "/search",
        "Admin": "/admin",
    }


def create_app(tenant_id: str) -> Flask:
    cfg = TENANTS[tenant_id]
    app = Flask(__name__)
    # In-process mutable copy so a run can actually change state.
    members: dict[str, Member] = {k: v for k, v in MEMBERS.items()}

    def fault() -> Fault:
        return active_fault(request.args.get("fault"), request.cookies.get(FAULT_COOKIE))

    @app.after_request
    def _persist_fault(response):
        """Make ?fault=... stick for the rest of the session."""
        chosen = request.args.get("fault")
        if chosen is not None:
            if chosen in ("", Fault.NONE):
                response.delete_cookie(FAULT_COOKIE)
            else:
                response.set_cookie(FAULT_COOKIE, chosen, samesite="Lax")
        return response

    def page(template: str, title: str, **kw):
        return render_template(template, cfg=cfg, page_title=title, **kw)

    def app_error(detail: str, status: int = 500):
        return page(
            "apperror.html", "Error",
            detail=detail, reference=f"ERR-{uuid.uuid4().hex[:8].upper()}",
        ), status

    def maybe_timeout(resume_url: str):
        """Session expiry is checked before the real handler runs."""
        if fault() is Fault.SESSION_TIMEOUT and request.args.get("reauth") != "1":
            return page("timeout.html", "Session Expired", resume_url=resume_url)
        return None

    # ---------------------------------------------------------------- routes

    @app.get("/")
    def root():
        return page("frameset.html", "Core Servicing")

    @app.get("/nav")
    def nav():
        return page("nav.html", "Navigation", nav_links=_nav_links(cfg))

    @app.get("/search")
    def search():
        return page("search.html", "Member Search", message=request.args.get("message"))

    @app.get("/search/run")
    def search_run():
        f = fault()
        if f is Fault.APP_ERROR_500:
            return app_error("Unhandled exception in MemberSearchServlet.")
        if (t := maybe_timeout(url_for("search"))) is not None:
            return t
        if f is Fault.SLOW_LOAD:
            time.sleep(SLOW_LOAD_SECONDS)

        query = request.args.get("mid", "")
        member = None if f is Fault.MEMBER_NOT_FOUND else find_member(query)
        return page("results.html", "Search Results", member=member, query=query)

    @app.get("/member/<member_id>")
    def member_detail(member_id: str):
        f = fault()
        if f is Fault.APP_ERROR_500:
            return app_error("Null reference in MemberDetailController.")
        if f is Fault.PERMISSION_DENIED:
            return page(
                "apperror.html", "Access Denied",
                detail="You do not have permission to view this member record.",
                reference=f"SEC-{uuid.uuid4().hex[:8].upper()}",
            ), 403
        if (t := maybe_timeout(url_for("member_detail", member_id=member_id))) is not None:
            return t
        if f is Fault.SLOW_LOAD:
            time.sleep(SLOW_LOAD_SECONDS)

        member = find_member(member_id)
        if member is None:
            return page("results.html", "Search Results", member=None, query=member_id)

        notice = None
        if f is Fault.ACCOUNT_CLOSED or member.status == "CLOSED":
            notice = "This member record is closed. Servicing actions are unavailable."
        return page("member.html", "Member Detail", member=member, notice=notice)

    @app.get("/member/<member_id>/subaccount/new")
    def subaccount_new(member_id: str):
        member = find_member(member_id)
        if member is None:
            return page("results.html", "Search Results", member=None, query=member_id)
        if cfg.has_terms_interstitial and request.args.get("acked") != "1":
            return page("terms.html", "Disclosure", member=member)
        if member.status == "CLOSED":
            return page(
                "subaccount_new.html", "Open Sub-Account", member=member,
                error="Cannot open a sub-account on a closed member record.",
            )
        return page("subaccount_new.html", "Open Sub-Account", member=member, error=None)

    @app.post("/member/<member_id>/subaccount/submit")
    def subaccount_submit(member_id: str):
        f = fault()
        member = find_member(member_id)
        if member is None:
            return page("results.html", "Search Results", member=None, query=member_id)
        if f is Fault.APP_ERROR_500:
            return app_error("Transaction rollback in SubAccountService.")
        if f is Fault.PERMISSION_DENIED:
            return page(
                "apperror.html", "Access Denied",
                detail="Your role may not open sub-accounts.",
                reference=f"SEC-{uuid.uuid4().hex[:8].upper()}",
            ), 403

        nickname = (request.form.get("nickname") or "").strip()
        deposit = (request.form.get("deposit") or "").strip()
        kind = request.form.get("kind") or "Savings"

        error = None
        if f is Fault.VALIDATION_ERROR:
            error = f"{cfg.label_initial_deposit} must be at least $25.00."
        elif not nickname:
            error = f"{cfg.label_account_nickname} is required."
        elif not deposit.replace("$", "").replace(",", "").replace(".", "").isdigit():
            error = f"{cfg.label_initial_deposit} must be a dollar amount."
        if error:
            return page("subaccount_new.html", "Open Sub-Account", member=member, error=error)

        sub = SubAccount(next_subaccount_number(member), nickname, kind, f"${deposit.lstrip('$')}")
        member.sub_accounts.append(sub)
        return page(
            "confirm.html", "Confirmation", member=member, sub=sub,
            reference=f"REQ-{uuid.uuid4().hex[:8].upper()}",
        )

    @app.get("/admin")
    def admin():
        """Exists only so the allowlist has something real to block."""
        return page("apperror.html", "Admin", detail="Administration console.",
                    reference="ADMIN"), 200

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "tenant": cfg.tenant_id, "version": cfg.app_version}

    return app
