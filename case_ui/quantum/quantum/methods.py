"""
quantum/methods.py — the four ways to compute the size of the harm.

Each function returns a HeadOfLoss. None of these knows or cares about
recoverability — that is the constraints layer's job. Here we only answer
"how big was the loss on THIS basis".

Grounding rule: a method may only enter the headline if its figure is grounded
in the bundle. cost_basis and value_basis are grounded for Meridian v TechFlow;
market_basis and dcf_basis need data the bundle does not contain, so they are
emitted as grounded=False (illustrative) unless the caller supplies real inputs.
"""
from __future__ import annotations

from .models import Basis, Cited, HeadOfLoss


def cost_basis(wasted: Cited) -> HeadOfLoss:
    """Reliance / cost basis: sums actually spent and thrown away.

    This head is NOT loss of profit, so a loss-of-profit exclusion does not
    touch it — which is exactly why it tends to be the survivor."""
    return HeadOfLoss(
        name="Wasted expenditure",
        basis=Basis.COST,
        value=wasted,
        is_loss_of_profit=False,
        note="Reliance loss — sums paid under the contract and wasted.",
    )


def value_basis(lost_profit: Cited) -> HeadOfLoss:
    """Expectation / value basis: the profit the bargain would have produced.

    This is the headline-grabbing number, and the one most exposed to both
    causation arguments and a loss-of-profit exclusion clause."""
    return HeadOfLoss(
        name="Loss of profit",
        basis=Basis.VALUE,
        value=lost_profit,
        is_loss_of_profit=True,
        note="Expectation loss — profit the platform would have earned.",
    )


def market_basis(benchmark: Cited | None = None) -> HeadOfLoss:
    """Market / comparable basis: what the same harm costs a comparable party.

    The bundle holds no comparable-transaction data, so absent a caller-supplied
    benchmark this is emitted ungrounded (and the waterfall keeps it out of the
    headline). Wiring point for a real comparables dataset later."""
    val = benchmark or Cited(
        amount=0.0,
        grounded=False,
        quote="No comparable-transaction data in the bundle.",
    )
    return HeadOfLoss(
        name="Comparable-market loss",
        basis=Basis.MARKET,
        value=val,
        is_loss_of_profit=True,
        note="Needs external comparables — illustrative only unless supplied.",
    )


def dcf_basis(
    annual_cash_flows: list[float] | None = None,
    discount_rate: float = 0.10,
    source: Cited | None = None,
) -> HeadOfLoss:
    """Discounted cash flow: present value of the lost future cash stream.

    PV = sum( cf_t / (1+r)^t ).  If no cash flows are supplied the figure is
    ungrounded (the bundle has no multi-year projection), so it stays out of the
    headline. Supplying real flows + a sourced rate makes it grounded."""
    if annual_cash_flows:
        pv = sum(cf / (1 + discount_rate) ** (t + 1)
                 for t, cf in enumerate(annual_cash_flows))
        val = source or Cited(amount=pv, grounded=False)
        val.amount = round(pv, 2)
    else:
        val = Cited(amount=0.0, grounded=False,
                    quote="No multi-year cash-flow projection in the bundle.")
    return HeadOfLoss(
        name="DCF of lost cash flows",
        basis=Basis.DCF,
        value=val,
        is_loss_of_profit=True,
        note=f"PV at r={discount_rate:.0%} — illustrative unless flows supplied.",
    )
