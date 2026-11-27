"""Scenario module `retail_supplier_delay`.

Retail chain whose revenue declines although demand is stable. Root cause: one supplier
gradually extends delivery delays. The story is produced purely by mechanisms:

    supplier delay  ->  late arrival of purchase orders  ->  on-hand stock hits zero
    ->  purchase intents of real (stable) demand cannot be fulfilled  ->  smaller baskets /
    walk-outs (no transaction)  ->  availability complaints that name the missing product.

Demand (visits and purchase intents) is sampled independently of stock, so what-if runs with
the same seed share identical demand and differ only through the mechanism.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from ...contracts.io import ScenarioInput
from ...contracts.spec import (CausalStep, Column, Dashboard, Distractor, EvidenceQuery, Invariant, Knob, Meta,
                               ScenarioSpec, Story, Table, Widget)
from ...llm.client import get_llm
from ..effects import day_index, lognormal_noise, month_profile, ramp, streams, weekday_profile
from .base import GenContext, ScenarioModule, SimulationResult

# --------------------------------------------------------------------------- domain constants
# Order matters: with the round-robin assignment supplier 2 gets dairy (the default story supplier).
CATEGORIES = ["Pečivo", "Mléčné výrobky", "Ovoce a zelenina", "Nápoje", "Maso a uzeniny",
              "Trvanlivé potraviny", "Sladkosti a snacky", "Drogerie"]
CAT_DEMAND = np.array([1.5, 1.6, 1.3, 1.1, 1.0, 0.8, 0.8, 0.5])     # purchase propensity
CAT_ASSORTMENT = np.array([0.12, 0.15, 0.13, 0.13, 0.12, 0.13, 0.11, 0.11])
CAT_PRICE = [(9, 79), (14, 129), (19, 89), (12, 249), (39, 329), (15, 119), (15, 89), (35, 199)]
CAT_FRESH = [True, True, True, False, True, False, False, False]
# monthly demand-mix seasonality per category (Jan..Dec)
CAT_SEASON = {
    "Nápoje": [0.85, 0.85, 0.9, 1.0, 1.1, 1.3, 1.4, 1.35, 1.05, 0.9, 0.85, 1.05],
    "Sladkosti a snacky": [0.9, 0.95, 0.95, 1.05, 0.95, 0.9, 0.9, 0.9, 0.95, 1.0, 1.15, 1.6],
    "Ovoce a zelenina": [0.9, 0.9, 0.95, 1.0, 1.05, 1.15, 1.2, 1.15, 1.05, 0.95, 0.9, 0.95],
    "Pečivo": [1.0] * 11 + [1.15],
}
FOOTFALL_MONTH = [0.93, 0.95, 0.98, 1.0, 1.02, 1.0, 0.97, 0.98, 1.0, 1.02, 1.05, 1.18]
WEEKDAY = [0.92, 0.9, 0.95, 1.0, 1.15, 1.25, 0.83]
COMPLAINT_CATEGORIES = ["Nedostupnost zboží", "Kvalita zboží", "Cena", "Personál", "Fronty u pokladen", "Reklamace"]
BASE_COMPLAINT_MIX = np.array([0.08, 0.28, 0.18, 0.18, 0.18, 0.10])
VOLUME_VISITORS = {"small": 20, "medium": 45, "large": 90}

DEFAULT_PARAMS = {
    "safety_stock_days": 2.0,     # store replenishment policy (repairable)
    "min_facing": 4.0,            # minimal shelf stock per product
    "noise_level": 1.0,           # scales daily demand noise (repairable)
    "walkout_prob": 0.22,         # P(customer abandons the whole basket when an item is missing)
    "complaint_prob_miss": 0.015, # P(complaint | visit with a missing item)
    "complaint_prob_walkout": 0.06,
    "identified_share": 0.55,     # share of visits with loyalty card
    "items_per_visit": 2.2,
    "base_complaints_per_store_day": 0.12,
}


# --------------------------------------------------------------------------- CHEAP output schemas
class SupplierRow(BaseModel):
    supplier_id: int
    name: str
    city: str
    contact_email: str


class SupplierBatch(BaseModel):
    items: list[SupplierRow]


class ProductRow(BaseModel):
    name: str = Field(description="Product name incl. brand/package, unique")
    unit_price: float = Field(description="Typical shelf price in CZK")


class ProductBatch(BaseModel):
    items: list[ProductRow]


class StoreRow(BaseModel):
    name: str
    city: str
    region: str


class StoreBatch(BaseModel):
    items: list[StoreRow]


class NamePools(BaseModel):
    male_first: list[str]
    female_first: list[str]
    male_last: list[str]
    female_last: list[str]


class ComplaintTemplate(BaseModel):
    category: str
    text: str


class ComplaintTemplates(BaseModel):
    templates: list[ComplaintTemplate]


# --------------------------------------------------------------------------- helpers
def _col(name, type_="int", desc="", pk=False, fk=None, nullable=False, enum=None, mn=None, mx=None) -> Column:
    return Column(name=name, type=type_, description=desc, pk=pk, fk=fk, nullable=nullable,
                  enum_values=enum or [], min=mn, max=mx)


def _supplier_categories(n_suppliers: int) -> dict[int, list[int]]:
    """supplier_id -> list of category indexes."""
    out: dict[int, list[int]] = {i: [] for i in range(1, n_suppliers + 1)}
    if n_suppliers <= len(CATEGORIES):
        for j in range(len(CATEGORIES)):
            out[(j % n_suppliers) + 1].append(j)
    else:
        for i in range(1, n_suppliers + 1):
            out[i].append((i - 1) % len(CATEGORIES))
    return out


def _price_round(x: float) -> float:
    return max(0.9, math.floor(x) + 0.9)


class RetailSupplierDelay(ScenarioModule):
    id = "retail_supplier_delay"
    title = "Maloobchod – zpoždění dodavatele → výpadky zásob → pokles tržeb"
    description = ("Retail chain (stores, products, suppliers, customers). One supplier gradually delays deliveries, "
                   "causing stockouts of its products, lost purchases and availability complaints while demand "
                   "(footfall) stays stable. Includes seasonality and red herrings (store renovation, competitor "
                   "opening, one-off strike at another supplier, price increase in one category).")
    domain = "retail"
    repairable_params = {"safety_stock_days": (0.5, 5.0), "noise_level": (0.2, 2.0),
                         "complaint_prob_miss": (0.005, 0.1), "walkout_prob": (0.0, 0.5)}

    # ------------------------------------------------------------------ schema
    def tables(self) -> list[Table]:
        return [
            Table(name="suppliers", strategy="llm_dimension", row_count=7, flags=["dimension"],
                  description="Dodavatelé zboží, každý zásobuje 1–2 kategorie.",
                  columns=[_col("supplier_id", pk=True, desc="ID dodavatele"),
                           _col("name", "text", "Obchodní název"), _col("city", "text", "Sídlo"),
                           _col("contact_email", "text", "Kontaktní e-mail"),
                           _col("category_focus", "text", "Kategorie, které dodává"),
                           _col("contract_lead_time_days", "int", "Smluvní dodací lhůta (dny)", mn=1, mx=10),
                           _col("order_weekdays", "text", "Dny objednávek (0=Po … 6=Ne)")]),
            Table(name="products", strategy="llm_dimension", row_count=240, flags=["dimension"],
                  description="Sortiment; každý produkt má jednoho dodavatele.",
                  columns=[_col("product_id", pk=True), _col("sku", "text", "Kód zboží"),
                           _col("name", "text", "Název produktu"),
                           _col("category", "text", "Kategorie", enum=CATEGORIES),
                           _col("supplier_id", fk="suppliers.supplier_id", desc="Dodavatel"),
                           _col("unit_price", "float", "Základní prodejní cena (Kč)", mn=0.9, mx=1000),
                           _col("unit_cost", "float", "Nákupní cena (Kč)", mn=0.1, mx=1000),
                           _col("is_fresh", "bool", "Čerstvé zboží s krátkou trvanlivostí")]),
            Table(name="stores", strategy="llm_dimension", row_count=10, flags=["dimension"],
                  description="Prodejny sítě.",
                  columns=[_col("store_id", pk=True), _col("name", "text"), _col("city", "text"),
                           _col("region", "text"), _col("size_m2", "int", "Prodejní plocha", mn=200, mx=3000),
                           _col("opened_on", "date", "Datum otevření")]),
            Table(name="customers", strategy="llm_dimension", row_count=1500, flags=["dimension"],
                  description="Zákazníci s věrnostní kartou (část nákupů je anonymní).",
                  columns=[_col("customer_id", pk=True), _col("first_name", "text"), _col("last_name", "text"),
                           _col("email", "text"), _col("home_store_id", fk="stores.store_id"),
                           _col("loyalty_tier", "text", enum=["basic", "silver", "gold"]),
                           _col("registered_on", "date")]),
            Table(name="purchase_orders", strategy="simulation", flags=["fact", "story_affected"],
                  description="Objednávky prodejen u dodavatelů (periodická revize, doplnění do cílové zásoby).",
                  columns=[_col("po_id", pk=True), _col("supplier_id", fk="suppliers.supplier_id"),
                           _col("store_id", fk="stores.store_id"), _col("ordered_on", "date"),
                           _col("expected_delivery_on", "date"),
                           _col("status", "text", enum=["delivered", "open"])]),
            Table(name="purchase_order_lines", strategy="simulation", flags=["fact"],
                  description="Položky objednávek.",
                  columns=[_col("po_line_id", pk=True), _col("po_id", fk="purchase_orders.po_id"),
                           _col("product_id", fk="products.product_id"), _col("qty_ordered", mn=1),
                           _col("qty_received", mn=0)]),
            Table(name="deliveries", strategy="simulation", flags=["fact", "story_affected"],
                  description="Dodávky: plánované vs. skutečné datum.",
                  columns=[_col("delivery_id", pk=True), _col("po_id", fk="purchase_orders.po_id"),
                           _col("supplier_id", fk="suppliers.supplier_id"), _col("store_id", fk="stores.store_id"),
                           _col("planned_date", "date"), _col("delivered_date", "date"),
                           _col("delay_days", "int", "Zpoždění ve dnech", mn=0, mx=60)]),
            Table(name="inventory_snapshots", strategy="simulation", flags=["fact", "story_affected"],
                  description="Týdenní stav zásob (neděle večer) a počet dní bez zásoby za posledních 7 dní.",
                  columns=[_col("snapshot_id", pk=True), _col("snapshot_date", "date"),
                           _col("store_id", fk="stores.store_id"), _col("product_id", fk="products.product_id"),
                           _col("on_hand_qty", mn=0), _col("days_out_of_stock_7d", mn=0, mx=7)]),
            Table(name="stockout_events", strategy="simulation", flags=["event", "story_affected"],
                  description="Souvislá období, kdy produkt v prodejně chyběl.",
                  columns=[_col("stockout_id", pk=True), _col("store_id", fk="stores.store_id"),
                           _col("product_id", fk="products.product_id"), _col("start_date", "date"),
                           _col("end_date", "date"), _col("duration_days", mn=1)]),
            Table(name="footfall_daily", strategy="simulation", flags=["fact"],
                  description="Denní návštěvnost prodejen (čítač u vstupu) – měřítko poptávky.",
                  columns=[_col("footfall_id", pk=True), _col("store_id", fk="stores.store_id"),
                           _col("date", "date"), _col("visitors", mn=0)]),
            Table(name="transactions", strategy="simulation", flags=["fact", "story_affected"],
                  description="Uskutečněné nákupy (účtenky).",
                  columns=[_col("transaction_id", pk=True), _col("store_id", fk="stores.store_id"),
                           _col("customer_id", fk="customers.customer_id", nullable=True),
                           _col("ts", "datetime", "Čas nákupu"), _col("items_count", mn=1),
                           _col("total_amount", "float", "Celkem (Kč)", mn=0),
                           _col("payment_method", "text", enum=["card", "cash", "mobile"])]),
            Table(name="transaction_items", strategy="simulation", flags=["fact"],
                  description="Položky účtenek.",
                  columns=[_col("transaction_item_id", pk=True),
                           _col("transaction_id", fk="transactions.transaction_id"),
                           _col("product_id", fk="products.product_id"), _col("quantity", mn=1),
                           _col("unit_price", "float", mn=0), _col("line_total", "float", mn=0)]),
            Table(name="complaints", strategy="simulation", flags=["event", "story_affected"],
                  description="Stížnosti zákazníků; text vzniká ze šablon (CHEAP model) doplněných o skutečná fakta.",
                  columns=[_col("complaint_id", pk=True), _col("created_at", "datetime"),
                           _col("store_id", fk="stores.store_id"),
                           _col("customer_id", fk="customers.customer_id", nullable=True),
                           _col("product_id", fk="products.product_id", nullable=True),
                           _col("category", "text", enum=COMPLAINT_CATEGORIES),
                           _col("channel", "text", enum=["store", "web_form", "email", "phone"]),
                           _col("text", "text", "Text stížnosti"),
                           _col("status", "text", enum=["open", "resolved"])]),
        ]

    def dimension_tables(self) -> list[str]:
        return ["suppliers", "products", "stores", "customers"]

    def knobs(self, inp: Optional[ScenarioInput] = None) -> list[Knob]:
        start = dt.date.fromisoformat(inp.period_start) if inp else dt.date(2025, 1, 1)
        end = dt.date.fromisoformat(inp.period_end) if inp else dt.date(2026, 3, 31)
        delay_start = start + (end - start) * 45 // 100
        n_sup = (inp.entity_counts.get("suppliers") if inp else None) or 7
        intensity = inp.story_intensity if inp else 1.0
        visitors = VOLUME_VISITORS.get(inp.volume if inp else "medium", 45)
        return [
            Knob(name="supplier_id", label="Problémový dodavatel", type="entity", default="2", min=1, max=n_sup,
                 description="ID dodavatele, který začne dodávat pozdě", applies_to=["s1", "deliveries"]),
            Knob(name="delay_start_date", label="Začátek zpoždění", type="date", default=delay_start.isoformat(),
                 description="Od kdy se dodávky dodavatele začnou zpožďovat", applies_to=["s1"]),
            Knob(name="delay_severity", label="Závažnost zpoždění (dny)", type="float", default="6", min=0, max=15,
                 description="Maximální průměrné zpoždění dodávek v dnech po náběhu", applies_to=["s1", "s2"]),
            Knob(name="ramp_profile", label="Profil náběhu", type="enum", default="linear",
                 options=["step", "linear", "exponential", "sigmoid"],
                 description="Jak rychle zpoždění narůstá", applies_to=["s1"]),
            Knob(name="ramp_days", label="Délka náběhu (dny)", type="int", default="75", min=1, max=365,
                 description="Za kolik dní zpoždění dosáhne plné závažnosti", applies_to=["s1"]),
            Knob(name="story_intensity", label="Intenzita příběhu", type="float", default=str(intensity), min=0, max=2,
                 description="Násobitel příběhu; 0 = zdravý baseline bez příběhu", applies_to=["s1", "s2", "s3", "s4"]),
            Knob(name="visitors_per_store_day", label="Návštěvnost (lidí/den/prodejna)", type="int",
                 default=str(visitors), min=5, max=300, description="Objem dat – průměrná denní návštěvnost",
                 applies_to=["footfall_daily", "transactions"]),
            Knob(name="distractors_enabled", label="Distraktory zapnuty", type="bool", default="true",
                 description="Rekonstrukce, konkurence, stávka jiného dodavatele, zdražení nápojů",
                 applies_to=["d1", "d2", "d3", "d4"]),
        ]

    # ------------------------------------------------------------------ evidence & checks
    def evidence(self) -> list[EvidenceQuery]:
        return [
            EvidenceQuery(
                id="e_revenue", step_id="s3", title="Měsíční tržby sítě", chart="line", x="month",
                y=["revenue_k_czk"],
                expected="Tržby po začátku zpoždění klesají (sezónní výkyvy kolem Vánoc jsou distraktor).",
                sql="SELECT substr(ts,1,7) AS month, ROUND(SUM(total_amount)/1000.0,1) AS revenue_k_czk, "
                    "COUNT(*) AS transactions FROM transactions GROUP BY month ORDER BY month"),
            EvidenceQuery(
                id="e_demand", step_id="s3", title="Návštěvnost vs. nákupy (konverze)", chart="line", x="month",
                y=["visitors", "transactions", "conversion_pct"],
                expected="Návštěvnost (poptávka) je stabilní, klesá podíl návštěv končících nákupem.",
                sql="SELECT f.month, f.visitors, t.transactions, "
                    "ROUND(100.0*t.transactions/f.visitors,1) AS conversion_pct FROM "
                    "(SELECT substr(date,1,7) AS month, SUM(visitors) AS visitors FROM footfall_daily GROUP BY 1) f "
                    "JOIN (SELECT substr(ts,1,7) AS month, COUNT(*) AS transactions FROM transactions GROUP BY 1) t "
                    "USING(month) ORDER BY month"),
            EvidenceQuery(
                id="e_delays", step_id="s1", title="Průměrné zpoždění dodávek podle dodavatele", chart="line",
                x="month", y=["avg_delay_days"], series="supplier",
                expected="Jediný dodavatel má rostoucí zpoždění od data začátku; ostatní jsou stabilní "
                         "(krátký výkyv jiného dodavatele = stávka, distraktor).",
                sql="SELECT substr(d.planned_date,1,7) AS month, s.name AS supplier, "
                    "ROUND(AVG(d.delay_days),2) AS avg_delay_days FROM deliveries d "
                    "JOIN suppliers s ON s.supplier_id=d.supplier_id GROUP BY month, supplier ORDER BY month"),
            EvidenceQuery(
                id="e_stockouts", step_id="s2", title="Podíl dní bez zásoby podle dodavatele", chart="line",
                x="month", y=["oos_pct"], series="supplier",
                expected="Výpadky zásob rostou jen u produktů zpožďujícího se dodavatele.",
                sql="SELECT substr(i.snapshot_date,1,7) AS month, s.name AS supplier, "
                    "ROUND(100.0*SUM(i.days_out_of_stock_7d)/(7.0*COUNT(*)),1) AS oos_pct "
                    "FROM inventory_snapshots i JOIN products p ON p.product_id=i.product_id "
                    "JOIN suppliers s ON s.supplier_id=p.supplier_id GROUP BY 1,2 ORDER BY 1"),
            EvidenceQuery(
                id="e_chain_weekly", step_id="s2", title="Týdenní řetězec: zpoždění → výpadky (zasažený dodavatel)",
                chart="line", x="week", y=["avg_delay_days", "oos_pct", "revenue_k_czk"],
                expected="Zpoždění roste první, výpadky zásob s malým odstupem, tržby produktů dodavatele klesají.",
                sql="WITH w AS (SELECT strftime('%Y-%W', planned_date) AS wk, AVG(delay_days) AS delay "
                    "FROM deliveries WHERE supplier_id={{knob:supplier_id}} GROUP BY 1), "
                    "o AS (SELECT strftime('%Y-%W', snapshot_date) AS wk, "
                    "100.0*SUM(days_out_of_stock_7d)/(7.0*COUNT(*)) AS oos FROM inventory_snapshots i "
                    "JOIN products p ON p.product_id=i.product_id WHERE p.supplier_id={{knob:supplier_id}} GROUP BY 1), "
                    "r AS (SELECT strftime('%Y-%W', t.ts) AS wk, SUM(ti.line_total)/1000.0 AS rev "
                    "FROM transaction_items ti JOIN transactions t ON t.transaction_id=ti.transaction_id "
                    "JOIN products p ON p.product_id=ti.product_id WHERE p.supplier_id={{knob:supplier_id}} GROUP BY 1) "
                    "SELECT w.wk AS week, ROUND(w.delay,2) AS avg_delay_days, ROUND(o.oos,1) AS oos_pct, "
                    "ROUND(r.rev,1) AS revenue_k_czk FROM w JOIN o USING(wk) JOIN r USING(wk) ORDER BY 1"),
            EvidenceQuery(
                id="e_control", step_id="s3", title="Tržby: zasažený dodavatel vs. kontrolní skupina (index)",
                chart="line", x="month", y=["revenue_index"], series="group_name",
                expected="Index tržeb produktů zasaženého dodavatele padá, kontrolní skupina drží úroveň (jen sezónnost).",
                sql="WITH m AS (SELECT substr(t.ts,1,7) AS month, CASE WHEN p.supplier_id={{knob:supplier_id}} "
                    "THEN 'Zasažený dodavatel' ELSE 'Kontrolní skupina' END AS grp, SUM(ti.line_total) AS rev "
                    "FROM transaction_items ti JOIN transactions t ON t.transaction_id=ti.transaction_id "
                    "JOIN products p ON p.product_id=ti.product_id GROUP BY 1,2), "
                    "base AS (SELECT grp, AVG(rev) AS b FROM m WHERE month < substr('{{knob:delay_start_date}}',1,7) "
                    "GROUP BY grp) SELECT m.month, m.grp AS group_name, ROUND(100.0*m.rev/base.b,1) AS revenue_index "
                    "FROM m JOIN base USING(grp) ORDER BY m.month"),
            EvidenceQuery(
                id="e_stores", step_id="s3", title="Změna návštěvnosti a tržeb po začátku zpoždění podle prodejen",
                chart="bar", x="store", y=["footfall_change_pct", "revenue_change_pct"],
                expected="Tržby klesají ve všech prodejnách bez poklesu návštěvnosti; jediná prodejna s poklesem "
                         "návštěvnosti je ta s novou konkurencí (distraktor).",
                sql="WITH f AS (SELECT store_id, AVG(CASE WHEN date < '{{knob:delay_start_date}}' THEN visitors END) AS pre, "
                    "AVG(CASE WHEN date >= '{{knob:delay_start_date}}' THEN visitors END) AS post "
                    "FROM footfall_daily WHERE visitors > 0 GROUP BY store_id), "
                    "r AS (SELECT store_id, SUM(CASE WHEN ts < '{{knob:delay_start_date}}' THEN total_amount END) * 1.0 / "
                    "COUNT(DISTINCT CASE WHEN ts < '{{knob:delay_start_date}}' THEN substr(ts,1,10) END) AS pre, "
                    "SUM(CASE WHEN ts >= '{{knob:delay_start_date}}' THEN total_amount END) * 1.0 / "
                    "COUNT(DISTINCT CASE WHEN ts >= '{{knob:delay_start_date}}' THEN substr(ts,1,10) END) AS post "
                    "FROM transactions GROUP BY store_id) "
                    "SELECT st.name AS store, ROUND(100.0*(f.post/f.pre-1),1) AS footfall_change_pct, "
                    "ROUND(100.0*(r.post/r.pre-1),1) AS revenue_change_pct FROM f JOIN r USING(store_id) "
                    "JOIN stores st ON st.store_id=f.store_id ORDER BY revenue_change_pct"),
            EvidenceQuery(
                id="e_complaints", step_id="s4", title="Stížnosti podle kategorie", chart="stacked_bar",
                x="month", y=["complaints"], series="category",
                expected="Roste kategorie 'Nedostupnost zboží', ostatní kategorie jsou stabilní.",
                sql="SELECT substr(created_at,1,7) AS month, category, COUNT(*) AS complaints FROM complaints "
                    "GROUP BY 1,2 ORDER BY 1"),
            EvidenceQuery(
                id="e_complaint_supplier", step_id="s4",
                title="Stížnosti na nedostupnost podle dodavatele zmíněného produktu (za měsíc)", chart="bar",
                x="supplier", y=["per_month_before", "per_month_after"],
                expected="Nárůst stížností se týká produktů zasaženého dodavatele.",
                sql="WITH b AS (SELECT MIN(substr(ts,1,10)) AS a, MAX(substr(ts,1,10)) AS z FROM transactions) "
                    "SELECT s.name AS supplier, ROUND(SUM(c.created_at < '{{knob:delay_start_date}}') / "
                    "((julianday('{{knob:delay_start_date}}')-julianday(b.a))/30.44),1) AS per_month_before, "
                    "ROUND(SUM(c.created_at >= '{{knob:delay_start_date}}') / "
                    "((julianday(b.z)-julianday('{{knob:delay_start_date}}'))/30.44),1) AS per_month_after "
                    "FROM complaints c JOIN products p ON p.product_id=c.product_id "
                    "JOIN suppliers s ON s.supplier_id=p.supplier_id CROSS JOIN b "
                    "WHERE c.category='Nedostupnost zboží' GROUP BY s.name ORDER BY per_month_after DESC"),
        ]

    def invariants(self) -> list[Invariant]:
        return [
            Invariant(id="inv_delivery_after_plan", kind="compare", table="deliveries", column="delivered_date",
                      op=">=", other_column="planned_date", description="Dodávka nepřijde před plánovaným datem"),
            Invariant(id="inv_delay_consistent", kind="sql_zero_rows", description="delay_days = delivered - planned",
                      sql="SELECT delivery_id FROM deliveries WHERE delay_days != "
                          "CAST(julianday(delivered_date)-julianday(planned_date) AS INTEGER)"),
            Invariant(id="inv_plan_after_order", kind="sql_zero_rows", description="Plán dodání je po objednání",
                      sql="SELECT d.delivery_id FROM deliveries d JOIN purchase_orders o ON o.po_id=d.po_id "
                          "WHERE d.planned_date < o.ordered_on"),
            Invariant(id="inv_tx_total", kind="sql_zero_rows", description="Součet položek = celková částka účtenky",
                      sql="SELECT t.transaction_id FROM transactions t JOIN (SELECT transaction_id, SUM(line_total) s, "
                          "COUNT(*) n FROM transaction_items GROUP BY transaction_id) i USING(transaction_id) "
                          "WHERE ABS(i.s - t.total_amount) > 0.05 OR i.n != t.items_count"),
            Invariant(id="inv_line_total", kind="sql_zero_rows", description="line_total = quantity × unit_price",
                      sql="SELECT transaction_item_id FROM transaction_items WHERE ABS(line_total - quantity*unit_price) > 0.05"),
            Invariant(id="inv_qty_positive", kind="range", table="transaction_items", column="quantity", min=1,
                      description="Množství je kladné"),
            Invariant(id="inv_stock_nonneg", kind="range", table="inventory_snapshots", column="on_hand_qty", min=0,
                      description="Zásoba není záporná"),
            Invariant(id="inv_received_le_ordered", kind="compare", table="purchase_order_lines",
                      column="qty_received", op="<=", other_column="qty_ordered",
                      description="Dodáno nejvýše objednané množství"),
            Invariant(id="inv_stockout_dates", kind="compare", table="stockout_events", column="end_date", op=">=",
                      other_column="start_date", description="Konec výpadku po začátku"),
            Invariant(id="inv_no_sales_when_closed", kind="sql_zero_rows",
                      description="Žádné nákupy v den, kdy byla prodejna zavřená (návštěvnost 0)",
                      sql="SELECT t.transaction_id FROM transactions t JOIN footfall_daily f ON f.store_id=t.store_id "
                          "AND f.date=substr(t.ts,1,10) WHERE f.visitors = 0"),
            Invariant(id="inv_tx_le_visitors", kind="sql_zero_rows",
                      description="Počet nákupů za den nepřevýší návštěvnost",
                      sql="SELECT f.store_id, f.date FROM footfall_daily f JOIN (SELECT store_id, substr(ts,1,10) d, "
                          "COUNT(*) n FROM transactions GROUP BY 1,2) t ON t.store_id=f.store_id AND t.d=f.date "
                          "WHERE t.n > f.visitors"),
            Invariant(id="inv_tx_period", kind="sql_zero_rows", description="Transakce leží v období scénáře",
                      sql="SELECT transaction_id FROM transactions WHERE substr(ts,1,10) < '{{meta:period_start}}' "
                          "OR substr(ts,1,10) > '{{meta:period_end}}'"),
            Invariant(id="inv_complaint_product_text", kind="sql_zero_rows",
                      description="Stížnost na nedostupnost zmiňuje konkrétní produkt",
                      sql="SELECT complaint_id FROM complaints WHERE category='Nedostupnost zboží' AND product_id IS NULL"),
            Invariant(id="inv_customers_count", kind="row_count", table="customers",
                      description="Počet zákazníků odpovídá parametru"),
        ]

    def dashboard_widgets(self) -> list[Widget]:
        return [
            Widget(id="w_kpi_rev", title="Tržby celkem (tis. Kč)", chart="kpi", y=["value"],
                   sql="SELECT ROUND(SUM(total_amount)/1000.0,0) AS value FROM transactions"),
            Widget(id="w_kpi_tx", title="Počet nákupů", chart="kpi", y=["value"],
                   sql="SELECT COUNT(*) AS value FROM transactions"),
            Widget(id="w_kpi_oos", title="Výpadky zásob (události)", chart="kpi", y=["value"],
                   sql="SELECT COUNT(*) AS value FROM stockout_events"),
            Widget(id="w_kpi_compl", title="Stížnosti", chart="kpi", y=["value"],
                   sql="SELECT COUNT(*) AS value FROM complaints"),
            Widget(id="w_rev_cat", title="Tržby podle kategorie (měsíčně)", chart="stacked_bar", x="month",
                   y=["revenue_k_czk"], series="category", width=2,
                   sql="SELECT substr(t.ts,1,7) AS month, p.category, ROUND(SUM(ti.line_total)/1000.0,1) AS revenue_k_czk "
                       "FROM transaction_items ti JOIN transactions t ON t.transaction_id=ti.transaction_id "
                       "JOIN products p ON p.product_id=ti.product_id GROUP BY 1,2 ORDER BY 1"),
            Widget(id="w_delay_heat", title="Zpoždění dodávek: dodavatel × měsíc", chart="heatmap", x="month",
                   y=["avg_delay_days"], series="supplier", width=2,
                   sql="SELECT substr(d.planned_date,1,7) AS month, s.name AS supplier, ROUND(AVG(d.delay_days),2) "
                       "AS avg_delay_days FROM deliveries d JOIN suppliers s ON s.supplier_id=d.supplier_id "
                       "GROUP BY 1,2 ORDER BY 1"),
            Widget(id="w_top_oos", title="Nejčastěji chybějící produkty", chart="table",
                   sql="SELECT p.name AS product, s.name AS supplier, COUNT(*) AS events, SUM(e.duration_days) AS days "
                       "FROM stockout_events e JOIN products p ON p.product_id=e.product_id "
                       "JOIN suppliers s ON s.supplier_id=p.supplier_id GROUP BY 1,2 ORDER BY days DESC LIMIT 15"),
            Widget(id="w_payment", title="Způsob platby", chart="pie", x="payment_method", y=["n"],
                   sql="SELECT payment_method, COUNT(*) AS n FROM transactions GROUP BY 1"),
        ]

    # ------------------------------------------------------------------ spec template
    def default_spec(self, inp: ScenarioInput) -> ScenarioSpec:
        counts = {"suppliers": 7, "products": 240, "stores": 10, "customers": 1500}
        counts.update({k: int(v) for k, v in inp.entity_counts.items() if k in counts and v})
        tables = self.tables()
        for t in tables:
            if t.name in counts:
                t.row_count = counts[t.name]
        knobs = self.knobs(inp)
        ds = next(k.default for k in knobs if k.name == "delay_start_date")
        story = Story(
            logline="Tržby sítě klesají, ale poptávka je stabilní: dodavatel mléčných výrobků začal dodávat pozdě, "
                    "což způsobuje výpadky zásob, neuskutečněné nákupy a stížnosti zákazníků.",
            causal_chain=[
                CausalStep(id="s1", mechanism="Dodavatel postupně prodlužuje zpoždění dodávek",
                           affected_entities=["suppliers:{{knob:supplier_id}}"], tables=["deliveries", "purchase_orders"],
                           start=ds, ramp_profile="linear",
                           expected_impact="Průměrné zpoždění roste z ~0,3 dne na ~4 dny"),
                CausalStep(id="s2", mechanism="Opožděné dodávky vyčerpají zásobu jeho produktů",
                           affected_entities=["products of supplier {{knob:supplier_id}}"],
                           tables=["inventory_snapshots", "stockout_events"], start="after s1", ramp_profile="lagged",
                           lag_days=3, expected_impact="Podíl dní bez zásoby roste na desítky procent"),
                CausalStep(id="s3", mechanism="Zákazníci (stabilní návštěvnost) nemohou koupit chybějící zboží, "
                           "část odchází bez nákupu → klesají tržby",
                           affected_entities=["all stores", "products of supplier {{knob:supplier_id}}"],
                           tables=["transactions", "transaction_items", "footfall_daily"], start="after s2",
                           ramp_profile="lagged", expected_impact="Tržby zasažených produktů −30 až −50 %, celkem −8 až −15 %"),
                CausalStep(id="s4", mechanism="Roste počet stížností na nedostupnost zboží",
                           affected_entities=["customers", "products of supplier {{knob:supplier_id}}"],
                           tables=["complaints"], start="after s2", ramp_profile="lagged", lag_days=1,
                           expected_impact="Stížnosti 'Nedostupnost zboží' násobně vzrostou"),
            ],
            ground_truth_facts=[
                "Příčinou poklesu tržeb je dodavatel {{knob:supplier_id}}, nikoli pokles poptávky.",
                "Návštěvnost prodejen je stabilní (mimo sezónnost a jednu prodejnu s konkurencí).",
                "Výpadky zásob se týkají téměř výhradně produktů tohoto dodavatele.",
            ],
            distractors=[
                Distractor(id="d0", name="Sezónnost", kind="seasonality",
                           description="Vánoční špička, lednový propad, nápoje v létě, týdenní cyklus.",
                           tables=["transactions", "footfall_daily"]),
                Distractor(id="d1", name="Rekonstrukce prodejny", kind="event",
                           description="Jedna prodejna je ~9 dní zavřená (návštěvnost 0).", tables=["footfall_daily"]),
                Distractor(id="d2", name="Nová konkurence", kind="red_herring",
                           description="U jedné prodejny otevřel konkurent – skutečný lokální pokles návštěvnosti o ~18 %.",
                           tables=["footfall_daily", "transactions"]),
                Distractor(id="d3", name="Stávka u jiného dodavatele", kind="red_herring",
                           description="Jednorázové týdenní zpoždění jiného dodavatele, rychle odezní.",
                           tables=["deliveries"]),
                Distractor(id="d4", name="Zdražení nápojů", kind="red_herring",
                           description="Nápoje zdraží o 8 %, mírně klesne jejich prodej v kusech.",
                           tables=["transaction_items"]),
            ],
        )
        return ScenarioSpec(
            meta=Meta(name="Síť prodejen – zpoždění dodavatele", domain="retail", data_language=inp.data_language,
                      period_start=inp.period_start, period_end=inp.period_end, seed=inp.seed, archetype=self.id,
                      company=inp.company, environment=inp.environment),
            tables=tables, story=story, knobs=knobs, invariants=self.invariants(), evidence=self.evidence(),
            dashboard=Dashboard(title="Přehled sítě prodejen", widgets=self.dashboard_widgets()),
        )

    # ------------------------------------------------------------------ dimensions (CHEAP)
    async def build_dimensions(self, spec: ScenarioSpec, ctx: GenContext) -> dict[str, pd.DataFrame]:
        llm = get_llm()
        seed = spec.meta.seed
        rs = streams(seed, "dim_suppliers", "dim_products", "dim_stores", "dim_customers")
        n_sup = spec.table("suppliers").row_count or 7
        n_prod = spec.table("products").row_count or 240
        n_store = spec.table("stores").row_count or 10
        n_cust = spec.table("customers").row_count or 1500
        lang = spec.meta.data_language
        company = (spec.meta.company or "")[:600]
        env = (spec.meta.environment or "")[:600]
        notes = " ".join(ctx.notes.get(t, "") for t in self.dimension_tables()).strip()

        sup_cats = _supplier_categories(n_sup)
        ctx.progress("Dodavatelé (CHEAP)", 0.02)
        sup_desc = "\n".join(f"- supplier_id {i}: categories {', '.join(CATEGORIES[c] for c in cats)}"
                             for i, cats in sup_cats.items())
        sup = await llm.structured(
            role="cheap", task="dim_suppliers", schema=SupplierBatch,
            prompt=(f"Company: {company}\nEnvironment: {env}\nData language: {lang}\n"
                    f"Generate realistic supplier companies (fictional, not real brands) for a retail chain. "
                    f"One item per supplier_id below, name must fit its categories. Use a city in the country of the "
                    f"company and a plausible contact e-mail on the company's domain.\n{sup_desc}\n{notes}"),
            mock_context={"suppliers": {i: [CATEGORIES[c] for c in cats] for i, cats in sup_cats.items()}},
        )
        sup_rows = {r.supplier_id: r for r in sup.items}
        lead_rng = rs["dim_suppliers"]
        suppliers = []
        for i, cats in sup_cats.items():
            r = sup_rows.get(i) or SupplierRow(supplier_id=i, name=f"Dodavatel {i}", city="Praha",
                                               contact_email=f"obchod@dodavatel{i}.cz")
            fresh = any(CAT_FRESH[c] for c in cats)
            lead = int(lead_rng.integers(1, 3)) if fresh else int(lead_rng.integers(3, 6))
            weekdays = [0, 3] if fresh else [int(lead_rng.integers(0, 5))]
            suppliers.append({"supplier_id": i, "name": r.name.strip()[:120], "city": r.city.strip()[:60],
                              "contact_email": r.contact_email.strip()[:120],
                              "category_focus": ", ".join(CATEGORIES[c] for c in cats),
                              "contract_lead_time_days": lead,
                              "order_weekdays": ",".join(map(str, weekdays))})
        df_sup = pd.DataFrame(suppliers)

        # products: per (supplier, category) batches
        ctx.progress("Produkty (CHEAP)", 0.05)
        pairs = [(i, c) for i, cats in sup_cats.items() for c in cats]
        cat_supplier_count = {c: sum(1 for i, cc in pairs if cc == c) for _, c in pairs}
        weights = np.array([CAT_ASSORTMENT[c] / cat_supplier_count[c] for _, c in pairs])
        alloc = np.maximum(3, np.floor(weights / weights.sum() * n_prod)).astype(int)
        while alloc.sum() > n_prod:
            alloc[np.argmax(alloc)] -= 1
        while alloc.sum() < n_prod:
            alloc[np.argmax(weights - alloc / n_prod)] += 1

        async def product_batch(sid: int, c: int, n: int):
            out: list[ProductRow] = []
            chunk = 40
            for b in range(0, n, chunk):
                k = min(chunk, n - b)
                lo, hi = CAT_PRICE[c]
                res = await llm.structured(
                    role="cheap", task="dim_products", schema=ProductBatch,
                    prompt=(f"Data language: {lang}. Retail chain: {company[:200]}\n"
                            f"Generate {k} distinct grocery/retail products in category '{CATEGORIES[c]}' supplied by "
                            f"'{df_sup.loc[df_sup.supplier_id == sid, 'name'].iloc[0]}'. Include package size in the name "
                            f"(e.g. '1 l', '500 g'). Typical shelf price between {lo} and {hi} CZK. "
                            f"Batch {b // chunk + 1}: avoid names from other batches by varying brand/variant.\n{notes}"),
                    mock_context={"category": CATEGORIES[c], "n": k, "offset": b, "supplier_id": sid,
                                  "price_range": [lo, hi]},
                )
                out.extend(res.items[:k])
            return sid, c, out

        import asyncio
        results = await asyncio.gather(*[product_batch(sid, c, int(n)) for (sid, c), n in zip(pairs, alloc) if n > 0])
        prng = rs["dim_products"]
        products = []
        seen_names: set[str] = set()
        for sid, c, rows in results:
            lo, hi = CAT_PRICE[c]
            for r in rows:
                name = r.name.strip()[:120] or f"{CATEGORIES[c]} {len(products) + 1}"
                base_name = name
                k = 2
                while name.lower() in seen_names:
                    name = f"{base_name} ({k})"
                    k += 1
                seen_names.add(name.lower())
                price = _price_round(float(np.clip(r.unit_price if r.unit_price > 0 else (lo + hi) / 2, lo, hi)))
                products.append({"name": name, "category": CATEGORIES[c], "supplier_id": sid, "unit_price": price,
                                 "unit_cost": round(price * float(prng.uniform(0.58, 0.8)), 2),
                                 "is_fresh": CAT_FRESH[c]})
        # top up if the model returned fewer rows than requested
        while len(products) < n_prod:
            sid, c = pairs[len(products) % len(pairs)]
            lo, hi = CAT_PRICE[c]
            price = _price_round((lo + hi) / 2)
            products.append({"name": f"{CATEGORIES[c]} – varianta {len(products) + 1}", "category": CATEGORIES[c],
                             "supplier_id": sid, "unit_price": price, "unit_cost": round(price * 0.7, 2),
                             "is_fresh": CAT_FRESH[c]})
        df_prod = pd.DataFrame(products[:n_prod])
        df_prod.insert(0, "product_id", np.arange(1, len(df_prod) + 1))
        df_prod.insert(1, "sku", [f"SKU-{CATEGORIES.index(c) + 1:02d}{i:05d}" for i, c in
                                  zip(df_prod.product_id, df_prod.category)])

        # stores
        ctx.progress("Prodejny (CHEAP)", 0.08)
        st = await llm.structured(
            role="cheap", task="dim_stores", schema=StoreBatch,
            prompt=(f"Company: {company}\nEnvironment: {env}\nData language: {lang}\n"
                    f"Generate {n_store} stores of this chain: store name (brand + location), city and region. "
                    f"Cities should be plausible for the company's region, a few cities may host 2 stores.\n{notes}"),
            mock_context={"n": n_store},
        )
        srng = rs["dim_stores"]
        start = dt.date.fromisoformat(spec.meta.period_start)
        stores = []
        for i in range(n_store):
            r = st.items[i] if i < len(st.items) else StoreRow(name=f"Prodejna {i + 1}", city="Praha", region="Praha")
            stores.append({"store_id": i + 1, "name": r.name.strip()[:120], "city": r.city.strip()[:60],
                           "region": r.region.strip()[:60], "size_m2": int(srng.integers(450, 1600)),
                           "opened_on": (start - dt.timedelta(days=int(srng.integers(400, 6000)))).isoformat()})
        df_store = pd.DataFrame(stores)

        # customers: CHEAP gives name pools, combined deterministically
        ctx.progress("Zákazníci (CHEAP)", 0.1)
        pools = await llm.structured(
            role="cheap", task="dim_customer_names", schema=NamePools,
            prompt=(f"Data language: {lang}. Country context: {env[:200] or company[:200]}\n"
                    "Return 40 common male first names, 40 female first names, 40 male surnames and the 40 matching "
                    "female surname forms (same order, e.g. Czech 'Novák' -> 'Nováková'). No duplicates."),
            mock_context={},
        )
        crng = rs["dim_customers"]
        fsize = (df_store.size_m2.to_numpy() / df_store.size_m2.mean()) ** 0.7
        home = crng.choice(df_store.store_id.to_numpy(), size=n_cust, p=fsize / fsize.sum())
        female = crng.random(n_cust) < 0.53
        mf, ff = pools.male_first or ["Jan"], pools.female_first or ["Jana"]
        ml, fl = pools.male_last or ["Novák"], pools.female_last or ["Nováková"]
        li = crng.integers(0, max(len(ml), 1), size=n_cust)
        first = np.where(female, np.array(ff)[crng.integers(0, len(ff), n_cust)],
                         np.array(mf)[crng.integers(0, len(mf), n_cust)])
        last = np.where(female, np.array(fl)[li % len(fl)], np.array(ml)[li % len(ml)])
        reg_days = crng.integers(30, 2500, n_cust)
        df_cust = pd.DataFrame({
            "customer_id": np.arange(1, n_cust + 1), "first_name": first, "last_name": last,
            "email": [f"{_ascii(f)}.{_ascii(l)}{i}@email.cz" for i, (f, l) in enumerate(zip(first, last), 1)],
            "home_store_id": home,
            "loyalty_tier": crng.choice(["basic", "silver", "gold"], size=n_cust, p=[0.65, 0.25, 0.10]),
            "registered_on": [(start - dt.timedelta(days=int(d))).isoformat() for d in reg_days],
        })

        # complaint text templates (CHEAP) – filled with real facts during simulation
        ctx.progress("Šablony stížností (CHEAP)", 0.12)
        tmpl = await llm.structured(
            role="cheap", task="complaint_templates", schema=ComplaintTemplates,
            prompt=(f"Data language: {lang}. Retail chain: {company[:200]}\n"
                    "Write realistic short customer complaints (1-2 sentences, varied tone, some informal) for these "
                    f"categories: {', '.join(COMPLAINT_CATEGORIES)}. 8 templates for 'Nedostupnost zboží', 3 for each "
                    "other category. Use placeholders {product} (product name), {store} (store name) and for "
                    "'Nedostupnost zboží' sometimes {days} (number of days the product is missing). "
                    "Every 'Nedostupnost zboží' template must contain {product}. Use the category names exactly."),
            mock_context={},
        )
        templates: dict[str, list[str]] = {c: [] for c in COMPLAINT_CATEGORIES}
        for t in tmpl.templates:
            if t.category in templates and _template_ok(t.text, t.category):
                templates[t.category].append(t.text.strip()[:400])
        from ...presets.mock_data import COMPLAINT_TEMPLATES
        for c in COMPLAINT_CATEGORIES:
            if not templates[c]:
                templates[c] = list(COMPLAINT_TEMPLATES[c])
        return {"suppliers": df_sup, "products": df_prod, "stores": df_store, "customers": df_cust,
                "_complaint_templates": templates}

    # ------------------------------------------------------------------ simulation
    def simulate(self, spec: ScenarioSpec, dims: dict[str, Any], ctx: GenContext) -> SimulationResult:
        kv = ctx.knobs
        prm = {**DEFAULT_PARAMS, **{k: float(v) for k, v in ctx.params.items()}}
        seed = spec.meta.seed
        rs = streams(seed, "footfall", "basket", "times", "customers", "walkout", "orders", "delays",
                     "base_delay", "complaints", "complaint_text", "distractors", "payment", "receive", "browsers")
        suppliers: pd.DataFrame = dims["suppliers"]
        products: pd.DataFrame = dims["products"]
        stores: pd.DataFrame = dims["stores"]
        customers: pd.DataFrame = dims["customers"]
        templates: dict[str, list[str]] = dims["_complaint_templates"]

        dates = pd.date_range(spec.meta.period_start, spec.meta.period_end, freq="D")
        D, S, P = len(dates), len(stores), len(products)
        K = len(suppliers)
        story_sup = int(kv.get("supplier_id", 2))
        if story_sup not in set(suppliers.supplier_id):
            story_sup = int(suppliers.supplier_id.iloc[min(1, K - 1)])
        intensity = float(kv.get("story_intensity", 1.0))
        severity = float(kv.get("delay_severity", 4.0))
        profile = str(kv.get("ramp_profile", "linear"))
        ramp_days = int(kv.get("ramp_days", 75))
        delay_start = pd.Timestamp(str(kv.get("delay_start_date", dates[D * 45 // 100].date().isoformat())))
        visitors = float(kv.get("visitors_per_store_day", 45))
        distractors_on = bool(kv.get("distractors_enabled", True))

        cat_of = products.category.map({c: i for i, c in enumerate(CATEGORIES)}).to_numpy()
        sup_of = products.supplier_id.to_numpy()
        base_price = products.unit_price.to_numpy(dtype=float)
        sup_ids = suppliers.supplier_id.to_numpy()
        sup_pos = {int(s): i for i, s in enumerate(sup_ids)}
        sup_idx_of_prod = np.array([sup_pos[int(x)] for x in sup_of])

        # ---- distractors: chosen from the seed only (identical across what-if runs)
        rd = rs["distractors"]
        renov_store = int(rd.integers(S))
        renov_start = int(D * rd.uniform(0.15, 0.32))
        comp_store = int((renov_store + 1 + rd.integers(max(S - 1, 1))) % S) if S > 1 else 0
        comp_start = int(D * rd.uniform(0.42, 0.62))
        strike_sup = int(sup_ids[int(rd.integers(K))])
        if strike_sup == story_sup and K > 1:
            strike_sup = int(sup_ids[(sup_pos[strike_sup] + 1) % K])
        strike_start = int(D * rd.uniform(0.22, 0.4))
        price_cat = CATEGORIES.index("Nápoje")
        price_start = int(D * rd.uniform(0.3, 0.55))
        distractor_log: list[dict[str, Any]] = []

        # ---- footfall (demand) – independent of stock
        size = stores.size_m2.to_numpy(dtype=float)
        base_ff = visitors * (size / size.mean()) ** 0.7
        season = weekday_profile(dates, WEEKDAY) * month_profile(dates, FOOTFALL_MONTH)
        local = np.ones((S, D))
        if distractors_on:
            local[renov_store, renov_start:renov_start + 9] = 0.0
            local[comp_store, comp_start:] *= 0.82
            distractor_log += [
                {"id": "d1", "name": "Rekonstrukce prodejny", "store_id": renov_store + 1,
                 "store": stores.name.iloc[renov_store], "start": dates[renov_start].date().isoformat(),
                 "end": dates[min(renov_start + 8, D - 1)].date().isoformat()},
                {"id": "d2", "name": "Nová konkurence", "store_id": comp_store + 1, "store": stores.name.iloc[comp_store],
                 "start": dates[comp_start].date().isoformat(), "effect": "footfall -18 %"},
                {"id": "d3", "name": "Stávka u dodavatele", "supplier_id": strike_sup,
                 "supplier": suppliers.name.iloc[sup_pos[strike_sup]], "start": dates[strike_start].date().isoformat(),
                 "end": dates[min(strike_start + 6, D - 1)].date().isoformat(), "effect": "+4 dny zpoždění"},
                {"id": "d4", "name": "Zdražení nápojů", "category": "Nápoje", "start": dates[price_start].date().isoformat(),
                 "effect": "cena +8 %, poptávka -4 %"},
            ]
        noise = lognormal_noise(rs["footfall"], (S, D), 0.07 * prm["noise_level"])
        ff_mean = base_ff[:, None] * season[None, :] * local
        n_visits = rs["footfall"].poisson(ff_mean * noise)

        # ---- purchase-intent mix per day
        pop = rs["basket"].lognormal(0, 0.8, P) * CAT_DEMAND[cat_of]
        cat_month = np.ones((len(CATEGORIES), 12))
        for c, prof in CAT_SEASON.items():
            cat_month[CATEGORIES.index(c)] = prof
        months = dates.month.to_numpy() - 1
        weekday_arr = dates.day_of_week.to_numpy()
        mix = pop[None, :] * cat_month[cat_of][:, months].T  # D x P
        price_mult = np.ones((D, P))
        if distractors_on:
            mask = cat_of == price_cat
            mix[price_start:, mask] *= 0.96
            price_mult[price_start:, mask] = 1.08
        probs = mix / mix.sum(axis=1, keepdims=True)
        items_per_visit = prm["items_per_visit"]

        # expected daily demand per store/product/month for the ordering policy (planner's forecast)
        avg_q = 1.0 / 0.65
        month_mix = np.stack([probs[months == m].mean(axis=0) if (months == m).any() else probs.mean(axis=0)
                              for m in range(12)])  # 12 x P
        mu = (base_ff[:, None, None] * np.array(FOOTFALL_MONTH)[None, :, None] * (1 + items_per_visit) * avg_q
              * month_mix[None, :, :])  # S x 12 x P

        # ---- ordering policy setup
        lead = dict(zip(suppliers.supplier_id.astype(int), suppliers.contract_lead_time_days.astype(int)))
        order_days = {int(s): [int(x) for x in str(w).split(",") if x != ""]
                      for s, w in zip(suppliers.supplier_id, suppliers.order_weekdays)}
        review = {s: 7.0 / max(len(w), 1) for s, w in order_days.items()}
        safety = prm["safety_stock_days"]
        prod_by_sup = {int(s): np.flatnonzero(sup_of == s) for s in sup_ids}
        base_pack = np.where(base_price < 40, 6, np.where(base_price < 120, 4, 2))
        # slow movers are ordered by the piece; fast movers in case packs
        weekly = mu.mean(axis=1) * 7  # S x P
        pack = np.clip(np.minimum(base_pack[None, :], np.floor(weekly / 4)), 1, None)

        def target_level(s: int, sid: int, m: int) -> np.ndarray:
            idx = prod_by_sup[sid]
            cover = review[sid] + lead[sid] + safety
            # shelf presentation minimum: at least `min_facing` units even for slow movers
            return np.maximum(mu[s, m, idx] * cover, prm["min_facing"])

        on_hand = np.zeros((S, P))
        for sid in prod_by_sup:
            for s in range(S):
                on_hand[s, prod_by_sup[sid]] = np.ceil(target_level(s, sid, months[0]))
        on_order = np.zeros((S, P))
        arrivals: dict[int, list[int]] = {}

        # story delay as a function of planned delivery date
        t_story = day_index(dates, delay_start)
        story_delay_curve = intensity * severity * ramp(t_story, ramp_days, profile)  # per day index

        po_rows: list[tuple] = []        # po_id, supplier_id, store_id, ordered_d, planned_d, actual_d
        po_lines: list[np.ndarray] = []  # arrays [po_id, product_idx, qty_ord, qty_recv]
        po_injected: list[float] = []

        # ---- per-day buffers
        tx_parts, item_parts, miss_parts = [], [], []
        oos_day = np.zeros((D, S, P), dtype=bool)
        streak = np.zeros((S, P), dtype=int)
        snapshots: list[tuple[int, np.ndarray, np.ndarray]] = []
        oos_week = np.zeros((S, P), dtype=int)
        lost_units = np.zeros((D, K))
        demand_units = np.zeros((D, K))
        lost_rev = np.zeros(D)
        walkout_rev = np.zeros(D)

        cust_by_store = [customers.customer_id.to_numpy()[customers.home_store_id.to_numpy() == sid]
                         for sid in stores.store_id]
        all_cust = customers.customer_id.to_numpy()
        cust_w = rs["customers"].gamma(1.2, 1.0, len(all_cust))
        cw_by_store = [cust_w[customers.home_store_id.to_numpy() == sid] for sid in stores.store_id]
        minute_w = _minute_weights()
        rb, rt, rw, rc = rs["basket"], rs["times"], rs["walkout"], rs["customers"]
        r_ord, r_del, r_bd, r_recv = rs["orders"], rs["delays"], rs["base_delay"], rs["receive"]
        po_id = 0

        for d in range(D):
            if d % 30 == 0:
                ctx.progress(f"Simulace: {dates[d].date()}", 0.15 + 0.6 * d / D)
            # morning arrivals
            for pid in arrivals.pop(d, []):
                lines = po_lines[pid - 1]
                s = po_rows[pid - 1][2] - 1
                on_hand[s, lines[:, 1]] += lines[:, 3]
                on_order[s, lines[:, 1]] -= lines[:, 2]
            day_missed = np.zeros((S, P), dtype=bool)
            price_now = _price_round_vec(base_price * price_mult[d])
            for s in range(S):
                nv = int(n_visits[s, d])
                if nv == 0:
                    continue
                ks = 1 + rb.poisson(items_per_visit, nv)
                tot = int(ks.sum())
                prods = rb.choice(P, size=tot, p=probs[d])
                qty = rb.geometric(0.65, tot)
                visit_of = np.repeat(np.arange(nv), ks)
                minutes = np.sort(rt.choice(len(minute_w), size=nv, p=minute_w)) + 420
                secs = rt.integers(0, 60, nv)
                ident = rc.random(nv) < prm["identified_share"]
                pool, pw = cust_by_store[s], cw_by_store[s]
                cust = np.full(nv, -1)
                if len(pool):
                    cust[ident] = rc.choice(pool, size=int(ident.sum()), p=pw / pw.sum())
                walk_draw = rw.random(nv)

                # fulfillment in arrival order: cumulative qty per product vs on-hand
                order = np.argsort(prods, kind="stable")
                sp, sq = prods[order], qty[order]
                cs = np.cumsum(sq)
                starts = np.r_[0, np.flatnonzero(np.diff(sp)) + 1]
                lens = np.diff(np.r_[starts, len(sp)])
                base = np.repeat(cs[starts] - sq[starts], lens)
                ok_sorted = (cs - base) <= on_hand[s, sp]
                ok = np.empty(tot, dtype=bool)
                ok[order] = ok_sorted
                miss = ~ok
                visit_miss = np.bincount(visit_of, weights=miss, minlength=nv) > 0
                walk = visit_miss & (walk_draw < prm["walkout_prob"])
                kept = ok & ~walk[visit_of]
                sold = np.bincount(prods[kept], weights=qty[kept], minlength=P)
                on_hand[s] -= sold
                # accounting for the manifest (hidden from the dataset)
                sup_idx = sup_idx_of_prod[prods]
                np.add.at(demand_units[d], sup_idx, qty)
                np.add.at(lost_units[d], sup_idx[miss], qty[miss])
                lost_rev[d] += float((qty[miss] * price_now[prods[miss]]).sum())
                walkout_rev[d] += float((qty[ok & walk[visit_of]] * price_now[prods[ok & walk[visit_of]]]).sum())
                day_missed[s, prods[miss]] = True

                has_tx = np.bincount(visit_of, weights=kept, minlength=nv) > 0
                vids = np.flatnonzero(has_tx)
                key0 = (d * S + s) * 100000
                if len(vids):
                    tx_parts.append((np.full(len(vids), s + 1), np.full(len(vids), d), minutes[vids], secs[vids],
                                     cust[vids], key0 + vids))
                    kk = np.flatnonzero(kept)
                    item_parts.append((key0 + visit_of[kk], prods[kk], qty[kk], price_now[prods[kk]]))
                mm = np.flatnonzero(miss)
                if len(mm):
                    vm, first_pos = np.unique(visit_of[mm], return_index=True)
                    miss_parts.append((np.full(len(vm), s), np.full(len(vm), d), minutes[vm], cust[vm],
                                       prods[mm][first_pos], walk[vm]))
            # end of day stock status
            oos_today = (on_hand <= 0) | day_missed
            oos_day[d] = oos_today
            streak = np.where(oos_today, streak + 1, 0)
            oos_week += oos_today
            if weekday_arr[d] == 6 or d == D - 1:
                snapshots.append((d, on_hand.copy(), oos_week.copy()))
                oos_week[:] = 0

            # replenishment: periodic review, order-up-to target level
            wd = weekday_arr[d]
            m = months[d]
            for sid, wds in order_days.items():
                if wd not in wds:
                    continue
                idx = prod_by_sup[sid]
                L = lead[sid]
                planned = d + L
                # delay realised by the supplier for this delivery
                base_delay = int(r_bd.choice([0, 1, 2], p=[0.82, 0.14, 0.04]))
                for s in range(S):
                    target = target_level(s, sid, m)
                    need = target - on_hand[s, idx] - on_order[s, idx]
                    pk = pack[s, idx]
                    q = np.ceil(np.maximum(need, 0) / pk) * pk
                    sel = q > 0
                    if not sel.any():
                        continue
                    po_id += 1
                    extra = 0.0
                    if sid == story_sup and planned < D:
                        inj = story_delay_curve[planned]
                        if inj > 0:
                            extra = max(0.0, inj + r_del.normal(0, 0.5 + 0.2 * inj))
                    po_injected.append(extra)
                    strike = 4 if (distractors_on and sid == strike_sup and strike_start <= planned < strike_start + 7) else 0
                    jitter = int(r_bd.random() < 0.1)
                    delay = base_delay * (r_bd.random() < 0.7) + jitter + int(round(extra)) + strike
                    actual = planned + int(delay)
                    recv = q[sel].copy()
                    short = r_recv.random(len(recv)) < 0.02
                    recv[short] = np.floor(recv[short] * r_recv.uniform(0.5, 0.95, short.sum()))
                    lines = np.column_stack([np.full(sel.sum(), po_id), idx[sel], q[sel], recv]).astype(np.int64)
                    po_lines.append(lines)
                    po_rows.append((po_id, sid, s + 1, d, planned, actual))
                    on_order[s, idx[sel]] += q[sel]
                    if actual < D:
                        arrivals.setdefault(actual, []).append(po_id)

        ctx.progress("Sestavení tabulek", 0.78)
        tables = self._assemble(spec, dates, stores, products, suppliers, customers, templates, rs,
                                n_visits, tx_parts, item_parts, miss_parts, po_rows, po_lines, oos_day,
                                snapshots, prm)
        manifest = self._manifest(spec, dates, kv, story_sup, suppliers, products, stores, delay_start, ramp_days,
                                  po_rows, po_injected, lost_units, demand_units, lost_rev, walkout_rev, oos_day,
                                  tables, distractor_log, prm)
        return SimulationResult(tables=tables, manifest=manifest)

    # ------------------------------------------------------------------ assembly
    def _assemble(self, spec, dates, stores, products, suppliers, customers, templates, rs, n_visits,
                  tx_parts, item_parts, miss_parts, po_rows, po_lines, oos_day, snapshots, prm):
        D, S, P = oos_day.shape
        date_str = np.array([x.date().isoformat() for x in dates])
        prod_ids = products.product_id.to_numpy()
        store_names = stores.name.to_numpy()
        prod_names = products.name.to_numpy()

        # transactions & items
        cols = list(zip(*tx_parts)) if tx_parts else [[np.array([], int)]] * 6
        tx = pd.DataFrame({n: np.concatenate(c) for n, c in
                           zip(["store_id", "day", "minute", "sec", "customer_id", "key"], cols)})
        tx = tx.sort_values(["day", "minute", "sec", "store_id"], kind="stable").reset_index(drop=True)
        tx["transaction_id"] = np.arange(1, len(tx) + 1)
        cols = list(zip(*item_parts)) if item_parts else [[np.array([], int)]] * 4
        items = pd.DataFrame({n: np.concatenate(c) for n, c in
                              zip(["key", "product_idx", "quantity", "unit_price"], cols)})
        items = items.groupby(["key", "product_idx", "unit_price"], as_index=False, sort=False)["quantity"].sum()
        items = items.merge(tx[["key", "transaction_id"]], on="key", how="inner")
        items["product_id"] = prod_ids[items.product_idx.to_numpy()]
        items["line_total"] = (items.quantity * items.unit_price).round(2)
        items = items.sort_values(["transaction_id", "product_id"]).reset_index(drop=True)
        items["transaction_item_id"] = np.arange(1, len(items) + 1)
        agg = items.groupby("transaction_id").agg(total_amount=("line_total", "sum"), items_count=("product_id", "size"))
        tx = tx.join(agg, on="transaction_id")
        tx["total_amount"] = tx.total_amount.round(2)
        hh = tx.minute // 60
        mi = tx.minute % 60
        tx["ts"] = [f"{date_str[d]} {h:02d}:{m:02d}:{s:02d}" for d, h, m, s in zip(tx.day, hh, mi, tx.sec)]
        rp = rs["payment"]
        frac = tx.day.to_numpy() / max(D - 1, 1)
        u = rp.random(len(tx))
        p_cash = 0.26 - 0.06 * frac
        p_mobile = 0.12 + 0.08 * frac
        tx["payment_method"] = np.where(u < p_cash, "cash", np.where(u < p_cash + p_mobile, "mobile", "card"))
        tx["customer_id"] = tx.customer_id.where(tx.customer_id > 0, None).astype("Int64")
        transactions = tx[["transaction_id", "store_id", "customer_id", "ts", "items_count", "total_amount",
                           "payment_method"]]
        transaction_items = items[["transaction_item_id", "transaction_id", "product_id", "quantity", "unit_price",
                                   "line_total"]].copy()
        transaction_items["quantity"] = transaction_items.quantity.astype(int)

        # footfall (counter counts visits + browsers who never intended to buy)
        browsers = rs["browsers"].poisson(0.08 * n_visits)
        visitors = n_visits + browsers
        ff = pd.DataFrame({"store_id": np.repeat(stores.store_id.to_numpy(), D),
                           "date": np.tile(date_str, S), "visitors": visitors.reshape(-1)})
        ff = ff.sort_values(["date", "store_id"]).reset_index(drop=True)
        ff.insert(0, "footfall_id", np.arange(1, len(ff) + 1))

        # purchase orders, lines, deliveries
        po = pd.DataFrame(po_rows, columns=["po_id", "supplier_id", "store_id", "ordered_d", "planned_d", "actual_d"])
        delivered = po.actual_d < D
        purchase_orders = pd.DataFrame({
            "po_id": po.po_id, "supplier_id": po.supplier_id, "store_id": po.store_id,
            "ordered_on": date_str[po.ordered_d], "expected_delivery_on": _date_any(dates, po.planned_d),
            "status": np.where(delivered, "delivered", "open")})
        lines = np.vstack(po_lines) if po_lines else np.zeros((0, 4), dtype=np.int64)
        pol = pd.DataFrame(lines, columns=["po_id", "product_idx", "qty_ordered", "qty_received"])
        pol["product_id"] = prod_ids[pol.product_idx]
        open_ids = set(po.po_id[~delivered])
        pol.loc[pol.po_id.isin(open_ids), "qty_received"] = 0
        pol.insert(0, "po_line_id", np.arange(1, len(pol) + 1))
        purchase_order_lines = pol[["po_line_id", "po_id", "product_id", "qty_ordered", "qty_received"]]
        dv = po[delivered].reset_index(drop=True)
        deliveries = pd.DataFrame({
            "delivery_id": np.arange(1, len(dv) + 1), "po_id": dv.po_id, "supplier_id": dv.supplier_id,
            "store_id": dv.store_id, "planned_date": _date_any(dates, dv.planned_d),
            "delivered_date": date_str[dv.actual_d], "delay_days": (dv.actual_d - dv.planned_d).astype(int)})

        # inventory snapshots
        snap_rows = []
        for d, oh, ow in snapshots:
            snap_rows.append(pd.DataFrame({
                "snapshot_date": date_str[d], "store_id": np.repeat(stores.store_id.to_numpy(), P),
                "product_id": np.tile(prod_ids, S), "on_hand_qty": oh.reshape(-1).astype(int),
                "days_out_of_stock_7d": ow.reshape(-1).astype(int)}))
        inv = pd.concat(snap_rows, ignore_index=True)
        inv.insert(0, "snapshot_id", np.arange(1, len(inv) + 1))

        # stockout events = contiguous runs of OOS days
        padded = np.concatenate([np.zeros((1, S, P), bool), oos_day, np.zeros((1, S, P), bool)])
        diff = np.diff(padded.astype(np.int8), axis=0)
        st_d, st_s, st_p = np.nonzero(diff == 1)
        en_d, en_s, en_p = np.nonzero(diff == -1)
        a = pd.DataFrame({"s": st_s, "p": st_p, "start": st_d}).sort_values(["s", "p", "start"]).reset_index(drop=True)
        b = pd.DataFrame({"s": en_s, "p": en_p, "end": en_d - 1}).sort_values(["s", "p", "end"]).reset_index(drop=True)
        ev = pd.concat([a, b[["end"]]], axis=1).sort_values(["start", "s", "p"]).reset_index(drop=True)
        stockout_events = pd.DataFrame({
            "stockout_id": np.arange(1, len(ev) + 1), "store_id": stores.store_id.to_numpy()[ev.s],
            "product_id": prod_ids[ev.p], "start_date": date_str[ev.start], "end_date": date_str[ev.end],
            "duration_days": (ev.end - ev.start + 1).astype(int)})

        # complaints: availability complaints come from real missed intents; others are background
        rc = rs["complaints"]
        rtx = rs["complaint_text"]
        comp = []
        if miss_parts:
            cols = list(zip(*miss_parts))
            mp = pd.DataFrame({n: np.concatenate(c) for n, c in
                               zip(["store_idx", "day", "minute", "customer_id", "product_idx", "walkout"], cols)})
            # customers rarely complain about a one-off gap, much more about chronic ones
            streaks = np.array([_streak_at(oos_day, d, s, p) for d, s, p in
                                zip(mp.day, mp.store_idx, mp.product_idx)])
            chronic = np.minimum(5.0, 0.1 + (streaks / 2.5) ** 2)
            prob = np.where(mp.walkout, prm["complaint_prob_walkout"], prm["complaint_prob_miss"]) * chronic
            mp["streak"] = streaks
            pick = mp[rc.random(len(mp)) < prob].reset_index(drop=True)
            lag = rc.geometric(0.55, len(pick)) - 1
            for r, lg in zip(pick.itertuples(), lag):
                day = min(r.day + int(lg), D - 1)
                # streak at the visit day = how many consecutive days the product has been missing
                days_missing = int(r.streak)
                comp.append((day, int(r.minute + rc.integers(0, 600)) % 1440, r.store_idx,
                             int(r.customer_id) if r.customer_id > 0 else None, int(r.product_idx),
                             "Nedostupnost zboží", max(days_missing, 1)))
        # background complaints (quality, price, staff, queues, returns, occasional availability)
        base_rate = prm["base_complaints_per_store_day"] * (n_visits / max(n_visits.mean(), 1e-9))
        nb = rc.poisson(base_rate)
        for s, d in zip(*np.nonzero(nb)):
            for _ in range(int(nb[s, d])):
                cat = COMPLAINT_CATEGORIES[int(rc.choice(len(COMPLAINT_CATEGORIES), p=BASE_COMPLAINT_MIX))]
                pidx = int(rc.integers(P)) if cat in ("Kvalita zboží", "Cena", "Reklamace", "Nedostupnost zboží") else None
                pool = customers.customer_id.to_numpy()[customers.home_store_id.to_numpy() == s + 1]
                cid = int(rc.choice(pool)) if (len(pool) and rc.random() < 0.6) else None
                comp.append((int(d), int(rc.integers(480, 1260)), int(s), cid, pidx, cat, 1))
        comp.sort(key=lambda x: (x[0], x[1], x[2]))
        rows = []
        for i, (d, minute, s, cid, pidx, cat, days) in enumerate(comp, 1):
            tl = templates[cat]
            text = tl[int(rtx.integers(len(tl)))]
            text = _fill(text, product=prod_names[pidx] if pidx is not None else "zboží", store=store_names[s],
                         days=days)
            channel = ["store", "web_form", "email", "phone"][int(rtx.choice(4, p=[0.35, 0.35, 0.2, 0.1]))]
            status = "open" if (D - d) < 10 and rtx.random() < 0.7 else ("resolved" if rtx.random() < 0.93 else "open")
            rows.append((i, f"{date_str[d]} {minute // 60:02d}:{minute % 60:02d}:00", int(stores.store_id.iloc[s]),
                         cid, int(prod_ids[pidx]) if pidx is not None else None, cat, channel, text, status))
        complaints = pd.DataFrame(rows, columns=["complaint_id", "created_at", "store_id", "customer_id", "product_id",
                                                 "category", "channel", "text", "status"])
        complaints["customer_id"] = complaints.customer_id.astype("Int64")
        complaints["product_id"] = complaints.product_id.astype("Int64")

        return {
            "suppliers": suppliers, "products": products, "stores": stores, "customers": customers,
            "purchase_orders": purchase_orders, "purchase_order_lines": purchase_order_lines,
            "deliveries": deliveries, "inventory_snapshots": inv, "stockout_events": stockout_events,
            "footfall_daily": ff, "transactions": transactions, "transaction_items": transaction_items,
            "complaints": complaints,
        }

    # ------------------------------------------------------------------ ground truth
    def _manifest(self, spec, dates, kv, story_sup, suppliers, products, stores, delay_start, ramp_days, po_rows,
                  po_injected, lost_units, demand_units, lost_rev, walkout_rev, oos_day, tables, distractor_log, prm):
        D, S, P = oos_day.shape
        month = np.array([x.strftime("%Y-%m") for x in dates])
        sup_ids = suppliers.supplier_id.to_numpy()
        sp = list(sup_ids).index(story_sup)
        affected_mask = products.supplier_id.to_numpy() == story_sup
        po = pd.DataFrame(po_rows, columns=["po_id", "supplier_id", "store_id", "o", "p", "a"])
        po["injected"] = po_injected
        po["delay"] = po.a - po.p
        po["month"] = month[np.minimum(po.p, D - 1)]
        x = po[po.supplier_id == story_sup].groupby("month").agg(injected=("injected", "mean"), actual=("delay", "mean"))
        o = po[po.supplier_id != story_sup].groupby("month").agg(others=("delay", "mean"))
        oos_aff = oos_day[:, :, affected_mask].mean(axis=(1, 2))
        oos_ctl = oos_day[:, :, ~affected_mask].mean(axis=(1, 2))
        dfm = pd.DataFrame({"month": month, "oos_aff": oos_aff, "oos_ctl": oos_ctl,
                            "lost_units_aff": lost_units[:, sp], "demand_units_aff": demand_units[:, sp],
                            "lost_units_ctl": lost_units.sum(1) - lost_units[:, sp],
                            "lost_rev": lost_rev, "walkout_rev": walkout_rev})
        monthly = dfm.groupby("month").agg(oos_affected_pct=("oos_aff", "mean"), oos_control_pct=("oos_ctl", "mean"),
                                           lost_units_affected=("lost_units_aff", "sum"),
                                           demand_units_affected=("demand_units_aff", "sum"),
                                           lost_units_control=("lost_units_ctl", "sum"),
                                           lost_revenue_czk=("lost_rev", "sum"),
                                           walkout_revenue_czk=("walkout_rev", "sum"))
        monthly[["oos_affected_pct", "oos_control_pct"]] *= 100
        monthly = monthly.join(x).join(o).reset_index().round(2)
        monthly = monthly.rename(columns={"injected": "injected_delay_days", "actual": "actual_delay_affected",
                                          "others": "actual_delay_others"})

        ds = delay_start.date().isoformat()
        full = (delay_start + pd.Timedelta(days=ramp_days)).date().isoformat()
        tx = tables["transactions"]
        ti = tables["transaction_items"].merge(products[["product_id", "supplier_id"]], on="product_id")
        ti["date"] = ti.transaction_id.map(dict(zip(tx.transaction_id, tx.ts.str[:10])))

        def per_day(df, col, lo, hi):
            m = (df.date >= lo) & (df.date < hi)
            days = max(len(pd.date_range(lo, hi)) - 1, 1)
            return float(df.loc[m, col].sum()) / days

        start, end = spec.meta.period_start, (dates[-1] + pd.Timedelta(days=1)).date().isoformat()
        aff = ti[ti.supplier_id == story_sup]
        ctl = ti[ti.supplier_id != story_sup]
        realized = {
            "revenue_per_day_pre": round(per_day(ti, "line_total", start, ds), 1),
            "revenue_per_day_post": round(per_day(ti, "line_total", full, end), 1) if full < end else None,
            "affected_revenue_per_day_pre": round(per_day(aff, "line_total", start, ds), 1),
            "affected_revenue_per_day_post": round(per_day(aff, "line_total", full, end), 1) if full < end else None,
            "control_revenue_per_day_pre": round(per_day(ctl, "line_total", start, ds), 1),
            "control_revenue_per_day_post": round(per_day(ctl, "line_total", full, end), 1) if full < end else None,
        }
        for k in ("revenue", "affected_revenue", "control_revenue"):
            pre, post = realized[f"{k}_per_day_pre"], realized[f"{k}_per_day_post"]
            realized[f"{k}_change_pct"] = round(100 * (post / pre - 1), 1) if pre and post else None
        comp = tables["complaints"]
        av = comp[comp.category == "Nedostupnost zboží"]
        realized["availability_complaints_pre"] = int((av.created_at < ds).sum())
        realized["availability_complaints_post"] = int((av.created_at >= ds).sum())

        sup_name = suppliers.name.iloc[sp]
        return {
            "archetype": self.id,
            "story": {
                "supplier_id": story_sup, "supplier_name": sup_name,
                "supplier_categories": suppliers.category_focus.iloc[sp],
                "affected_product_ids": products.product_id[affected_mask].astype(int).tolist(),
                "affected_product_count": int(affected_mask.sum()),
                "delay_start_date": ds, "full_severity_date": full,
                "delay_severity_days": float(kv.get("delay_severity", 4)), "ramp_profile": kv.get("ramp_profile"),
                "ramp_days": ramp_days, "story_intensity": float(kv.get("story_intensity", 1)),
                "affected_stores": "all",
            },
            "causal_chain": [
                {"step": "s1", "fact": f"Dodavatel {sup_name} (ID {story_sup}) zpožďuje dodávky od {ds}, plná závažnost "
                                       f"od {full}.", "tables": ["deliveries", "purchase_orders"]},
                {"step": "s2", "fact": "Zásoby jeho produktů docházejí – viz oos_affected_pct vs oos_control_pct.",
                 "tables": ["inventory_snapshots", "stockout_events"]},
                {"step": "s3", "fact": f"Tržby zasažených produktů {realized['affected_revenue_change_pct']} %, kontrolní "
                                       f"skupina {realized['control_revenue_change_pct']} % (po plném náběhu vs. před).",
                 "tables": ["transactions", "transaction_items", "footfall_daily"]},
                {"step": "s4", "fact": f"Stížnosti na nedostupnost: {realized['availability_complaints_pre']} před, "
                                       f"{realized['availability_complaints_post']} po začátku.",
                 "tables": ["complaints"]},
            ],
            "realized": realized,
            "monthly": monthly.to_dict(orient="records"),
            "distractors": distractor_log,
            "params": prm,
            "hidden_mechanics": "Demand is sampled independently of stock; lost units/revenue are not stored in any table.",
        }


    # ------------------------------------------------------------------ deterministic story signals
    def story_signals(self, manifest: dict[str, Any], knobs: dict[str, Any]) -> list[dict[str, Any]]:
        """Quantified per-step signals computed from the ground truth + realised data (used by report/validator)."""
        if not manifest:
            return []
        m = pd.DataFrame(manifest.get("monthly", []))
        st = manifest["story"]
        r = manifest["realized"]
        ds, full = st["delay_start_date"][:7], st["full_severity_date"][:7]
        pre = m[m.month < ds]
        post = m[m.month >= full] if (m.month >= full).any() else m[m.month >= ds]

        def mean(df, col):
            v = df[col].dropna() if col in df else pd.Series(dtype=float)
            return float(v.mean()) if len(v) else 0.0

        d_aff, d_oth = mean(post, "actual_delay_affected"), mean(post, "actual_delay_others")
        o_aff, o_ctl = mean(post, "oos_affected_pct"), mean(post, "oos_control_pct")
        o_aff_pre = mean(pre, "oos_affected_pct")
        months_pre = max(len(pre), 1)
        months_post = max(len(m) - len(pre), 1)
        c_pre = r["availability_complaints_pre"] / months_pre
        c_post = r["availability_complaints_post"] / months_post
        aff, ctl = r.get("affected_revenue_change_pct"), r.get("control_revenue_change_pct")
        return [
            {"step_id": "s1", "detectable": d_aff - d_oth >= 1.5, "evidence_ids": ["e_delays", "e_chain_weekly"],
             "metric": {"delay_affected_days": round(d_aff, 2), "delay_others_days": round(d_oth, 2)},
             "comment": f"Zpoždění zasaženého dodavatele {d_aff:.1f} dne vs. ostatní {d_oth:.1f} dne (po náběhu)."},
            {"step_id": "s2", "detectable": o_aff >= 1.8 * max(o_ctl, 0.1) and o_aff - o_aff_pre >= 5,
             "evidence_ids": ["e_stockouts", "e_chain_weekly"],
             "metric": {"oos_affected_pct": round(o_aff, 1), "oos_control_pct": round(o_ctl, 1),
                        "oos_affected_pre_pct": round(o_aff_pre, 1)},
             "comment": f"Dny bez zásoby: zasažené produkty {o_aff:.1f} % (před {o_aff_pre:.1f} %), kontrolní {o_ctl:.1f} %."},
            {"step_id": "s3", "detectable": aff is not None and ctl is not None and aff <= -8 and ctl - aff >= 8,
             "evidence_ids": ["e_control", "e_demand", "e_revenue", "e_stores"],
             "metric": {"affected_revenue_change_pct": aff, "control_revenue_change_pct": ctl,
                        "total_revenue_change_pct": r.get("revenue_change_pct")},
             "comment": f"Tržby zasažených produktů {aff} %, kontrolní skupina {ctl} %, celkem {r.get('revenue_change_pct')} %."},
            {"step_id": "s4", "detectable": c_post >= 1.5 * max(c_pre, 0.1) and c_post - c_pre >= 5,
             "evidence_ids": ["e_complaints", "e_complaint_supplier"],
             "metric": {"availability_complaints_per_month_pre": round(c_pre, 1),
                        "availability_complaints_per_month_post": round(c_post, 1)},
             "comment": f"Stížnosti na nedostupnost {c_pre:.0f}/měsíc před vs. {c_post:.0f}/měsíc po začátku."},
        ]


# --------------------------------------------------------------------------- small utilities
def _minute_weights() -> np.ndarray:
    m = np.arange(14 * 60)  # 07:00 .. 21:00
    w = 0.4 + np.exp(-((m - 60) ** 2) / (2 * 60**2)) * 0.6 + np.exp(-((m - 600) ** 2) / (2 * 90**2)) * 1.2 \
        + np.exp(-((m - 300) ** 2) / (2 * 60**2)) * 0.5
    return w / w.sum()


def _price_round_vec(x: np.ndarray) -> np.ndarray:
    return np.maximum(0.9, np.floor(x) + 0.9)


def _date_any(dates: pd.DatetimeIndex, idx) -> np.ndarray:
    start = dates[0]
    return np.array([(start + pd.Timedelta(days=int(i))).date().isoformat() for i in np.asarray(idx)])


def _streak_at(oos_day: np.ndarray, d: int, s: int, p: int) -> int:
    n = 0
    while d - n >= 0 and oos_day[d - n, s, p]:
        n += 1
    return n


def _fill(text: str, **kw) -> str:
    out = text
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _template_ok(text: str, category: str) -> bool:
    import re

    allowed = {"product", "store", "days"}
    used = set(re.findall(r"\{(\w+)\}", text))
    if used - allowed:
        return False
    if category == "Nedostupnost zboží" and "product" not in used:
        return False
    return 10 <= len(text) <= 400


def _ascii(s: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if c.isascii() and c.isalnum())


MODULE = RetailSupplierDelay()
