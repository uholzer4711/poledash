"""Insight narrative engine for /insights.

Turns the numeric ``insights_extras.compute_*`` results into short, localised
plain-language sentences — the thing that makes the page *answer questions*
instead of showing raw charts. Each sentence is built with ``t()`` so it follows
the app-wide language, and every block degrades gracefully (omitted) when there
isn't enough data.

The heavy, already-computed pieces (forecast / trip_cost / co2 / soh / the
scatter lists) are passed in by the ``/api/insights`` route so we don't re-query;
the cheap derived facts are pulled from ``insights_extras`` helpers here.
"""
from __future__ import annotations

from .i18n import t
from .insights_extras import (
    charge_price_split,
    cold_charge_throttle,
    cost_vs_petrol,
    month_over_month_cost,
    range_now_vs_then,
    speed_sweet_spot,
    temp_efficiency_delta,
)


def _s(text: str, tone: str = "neutral") -> dict:
    return {"text": text, "tone": tone}


def build_narrative(
    *,
    days: int,
    vin: str | None,
    forecast: dict,
    trip_cost: dict,
    co2: dict,
    soh: dict,
    scatter: list[dict],
    temp_scatter: list[dict],
    charge_temp_scatter: list[dict],
) -> dict:
    cost: list[dict] = []
    efficiency: list[dict] = []
    battery: list[dict] = []
    waste: list[dict] = []
    impact: list[dict] = []

    # --- Cost ---------------------------------------------------------------
    cvp = cost_vs_petrol(trip_cost, forecast)
    if cvp and cvp["saved_per_month"] > 0:
        cost.append(_s(t(
            "Charging costs ~€{ev}/100 km — a comparable petrol car ~€{petrol}. "
            "You save ~€{save}/month.",
            ev=cvp["ev_per_100km"], petrol=cvp["petrol_per_100km"],
            save=cvp["saved_per_month"]), "good"))
    mom = month_over_month_cost(vin=vin)
    if mom:
        d = mom["delta_pct"]
        if d <= -3:
            cost.append(_s(t("{pct}% cheaper per 100 km than the previous 30 days.",
                             pct=abs(d)), "good"))
        elif d >= 3:
            cost.append(_s(t("{pct}% more expensive per 100 km than the previous "
                             "30 days.", pct=d), "warn"))
        else:
            cost.append(_s(t("Cost per 100 km is steady vs the previous 30 days.")))
    split = charge_price_split(days=days, vin=vin)
    if split and "ac_ct" in split and "dc_ct" in split:
        cost.append(_s(t("Slow AC charging ~{ac}¢/kWh vs fast DC ~{dc}¢/kWh.",
                         ac=split["ac_ct"], dc=split["dc_ct"])))

    # --- Efficiency ---------------------------------------------------------
    avg = forecast.get("avg_kwh_per_100km")
    if avg is not None:
        tone = "good" if avg <= 17 else ("warn" if avg >= 20 else "neutral")
        efficiency.append(_s(t("Your average is {v} kWh/100 km.", v=avg), tone))
    td = temp_efficiency_delta(temp_scatter)
    if td and td["delta_pct"] >= 3:
        efficiency.append(_s(t(
            "Below 5 °C consumption is ~{pct}% higher than in mild weather "
            "(~{cold} vs ~{mild} Wh/km).",
            pct=td["delta_pct"], cold=td["cold_wh"], mild=td["mild_wh"]), "warn"))
    sweet = speed_sweet_spot(scatter)
    if sweet and sweet["best_high"]:
        if sweet["highway_delta_pct"] and sweet["highway_delta_pct"] >= 5:
            efficiency.append(_s(t(
                "Most efficient around {low}–{high} km/h (~{wh} Wh/km); a highway "
                "pace costs ~{hpct}% more.",
                low=sweet["best_low"], high=sweet["best_high"], wh=sweet["best_wh"],
                hpct=sweet["highway_delta_pct"])))
        else:
            efficiency.append(_s(t("Most efficient around {low}–{high} km/h "
                                   "(~{wh} Wh/km).",
                                   low=sweet["best_low"], high=sweet["best_high"],
                                   wh=sweet["best_wh"])))

    # --- Battery ------------------------------------------------------------
    if soh.get("enough_data"):
        battery.append(_s(t(
            "Estimated battery health ~{pct}% (~{kwh} kWh usable). The car reports "
            "no SoH — this is computed from charge/drive energy.",
            pct=soh["soh_pct"], kwh=format(soh["implied_kwh"], "g")), "good"))
    rng = range_now_vs_then(days=max(days, 120))
    if rng:
        d = rng["delta_pct"]
        if abs(d) < 4:
            battery.append(_s(t("Range at 100 % is steady at ~{now} km.",
                                now=rng["now_km"])))
        elif d < 0:
            battery.append(_s(t("Range at 100 % is ~{now} km, ~{pct}% below "
                                "earlier in the window (~{then} km).",
                                now=rng["now_km"], pct=abs(d), then=rng["then_km"]),
                              "warn"))
        else:
            battery.append(_s(t("Range at 100 % rose to ~{now} km (from ~{then} km) "
                                "— warmer weather.", now=rng["now_km"],
                                then=rng["then_km"]), "good"))
    cct = cold_charge_throttle(charge_temp_scatter)
    if cct and cct["drop_pct"] >= 10:
        battery.append(_s(t(
            "Below 5 °C the car throttles charging to ~{cold} kW (vs ~{warm} kW "
            "warm) — preconditioning before a fast charge helps.",
            cold=cct["cold_kw"], warm=cct["warm_kw"])))

    # --- Waste --------------------------------------------------------------
    # The real "waste" is the parasitic standby *rate*, aggregated over all
    # confirmed-parked climate-idle time (integer-SoC noise cancels) — not the
    # sparse, weakly-attributed discrete events the old summary surfaced.
    from .idle_drain import standby_baseline
    sb = standby_baseline(days=days, vin=vin)
    st = sb.get("standby")
    if st and st["kwh_per_month"] >= 0.1:
        waste.append(_s(t(
            "Parasitic standby ~{pct}%/day ≈ {kwh} kWh/month (~€{eur}/month).",
            pct=st["pct_per_day"], kwh=st["kwh_per_month"],
            eur=st["eur_per_month"]),
            "warn" if st["kwh_per_month"] >= 8 else "neutral"))
    pc = sb.get("preconditioning")
    if pc and pc["kwh"] >= 0.5:
        waste.append(_s(t(
            "~{kwh} kWh went into pre-conditioning while parked — comfort / "
            "pre-heating, usually range-positive, not waste.",
            kwh=pc["kwh"]), "neutral"))

    # --- Impact -------------------------------------------------------------
    if co2.get("saved_kg") is not None and co2.get("g_per_km") is not None:
        impact.append(_s(t(
            "~{saved} kg CO₂ saved vs a petrol car ({g} g/km on your grid mix).",
            saved=co2["saved_kg"], g=co2["g_per_km"]), "good"))

    # --- Summary: the 3 most useful, in priority order ----------------------
    summary: list[dict] = []
    if cvp and cvp["saved_per_month"] > 0:
        summary.append(cost[0])
    if soh.get("enough_data") and battery:
        summary.append(battery[0])
    if td and td["delta_pct"] >= 3:
        summary.append(_s(t("Biggest efficiency lever: cold weather (+{pct}% below "
                            "5 °C).", pct=td["delta_pct"]), "warn"))
    elif mom and mom["delta_pct"] <= -3:
        summary.append(cost[1] if len(cost) > 1 else cost[0])
    summary = summary[:3]

    return {
        "summary": summary,
        "cost": cost,
        "efficiency": efficiency,
        "battery": battery,
        "waste": waste,
        "impact": impact,
    }
