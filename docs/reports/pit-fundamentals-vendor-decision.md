# Principal Decision: PIT Fundamentals Vendor for the Value/Quality Factor Panel

**Prepared for:** Principal · **Date:** 2026-07-20 · **Status:** Decision-ready
**Question:** Replace/augment EODHD (current-snapshot fundamentals only) with a vendor that delivers **true point-in-time** US fundamentals, 2012→present, **including ~70 delisted S&P 500 names**, to build value (E/P, B/P, FCF yield) and quality (GP/A, accruals, margins) factors on a survivorship-bias-free panel.

**Decision rule (from the brief):** TRUE-PIT and DELISTED coverage are the two **disqualifier** axes — a fail on either poisons the factor family. History depth, price, and integration effort are **tiebreakers**. Price only decides between vendors that already clear both disqualifiers.

---

## 1. Comparison table

| Vendor | True-PIT? | Delisted fundamentals? | History (2012+ Q+A?) | Price (indiv/research seat) | Integration effort |
|---|---|---|---|---|---|
| **Sharadar SF1** | **PASS** — AR* dims are as-first-reported, time-indexed to SEC Form-10 filing date; excludes restatements; MR dims give restated view for reconstruction [1][2] | **PASS (strong)** — ~10,000 delisted names, same-grade fundamentals; headline feature; "nearly" survivorship-free [3][6][8] | **PASS** — from 1997 (QuantRocket: 1990); Q+A+TTM; **must buy Full-History tier** [7][8] | **~$69/mo** personal full-history [5]; pro full-history **quote-only** | **LOW** — Nasdaq Data Link Tables API, keyed REST/JSON, `dimension=ARQ`; add SF1→DAILY join; ~few days |
| **Tiingo** | **PASS (documented, unaudited)** — `asReported` flag + release-date `date` field; original + latest vintage (not full chain) [9][10] | **UNVERIFIED** — vendor gives no delisted verdict; free eval is DOW-30 only, cannot test without paying [9] | **PASS** — 1990s depth; **20+yr add-on tier required** (10yr tier fails) [9] | **~$80/mo** ($30 Power + $49.99 20yr add-on) [9][11] | **LOW** — closest match to EODHD adapter; add permaTicker resolve; ~1–2 days |
| **Intrinio** | **PASS (as-reported) / PARTIAL (standardized)** — reported financials verbatim from 10-K/10-Q, `filing_date` + `is_latest`, original + restated retained; no as-of query param [12][13] | **PASS (vendor-stated)** — "10,000+ active & delisted companies… eliminates survivorship bias" [14] | **PASS** — from 2007/2008 (XBRL mandate); Q+A [15] | **$150/mo** Individual (no redistribution) [16] | **MODERATE** — XBRL as-filed tags need a normalization layer; ~3–5 days |
| **FMP** | **PARTIAL** — timing axis safe (`acceptedDate` from EDGAR); **value axis NOT restatement-safe** — no as-of/vintage param, standardized fields can be overwritten by restatements [17][18] | **PARTIAL / UNVERIFIED** — delisted *reference* list exists; statement-level coverage for dead tickers unproven in docs — **go/no-go probe required** [19][20] | **PASS on Premium+** — 30yr; Q+A (Starter 5yr fails) [21] | **$59/mo** Premium (annual-billed) [22] | **LOW** — near drop-in vs EODHD; ~1–3 days |
| **SimFin** | **PARTIAL** — default datasets **explicitly not PIT** (latest restatement); `asreported`/statements_original path + Publish Date gives PIT construction, but only a **single** Restated Date field (no full vintage chain) [23][24] | **PARTIAL / likely-yes** — delisted retained by policy, but coverage **thinner/more nulls** than active; specific 70 names unverified [25][26] | **PASS** — from 2003; Q+A; as-reported archive depth to 2012 UNVERIFIED [23] | **$35/mo** BASIC (cheapest) [27] | **LOW** — v3 keyed REST/JSON, must target `original`/asreported path; small job |
| **EODHD** (current) | **FAIL** — restated values overwritten in place; no PIT/as-reported/version history [28][29] | **FAIL for panel** — pre-2018 delistings have **EOD price only, no fundamentals** — kills first half of the window [30] | Fine for survivors (1985); **fails via delisted + PIT gaps** [28] | $99.99/mo All-In-One; $59.99 standalone fundamentals [31] | **ZERO** (already wired) — but moot: no endpoint fixes the missing PIT history |

---

## 2. Ranked recommendation

### #1 — Adopt **Sharadar SF1** (Nasdaq Data Link). Clear winner.
It is the only vendor that **passes both disqualifier axes on documented, independently-corroborated evidence, not vendor marketing**:
- **PIT is structural, not a flag.** The AR* dimensions are a *separate* as-first-reported series indexed to the SEC Form-10 filing date; the worked Abbott (ABT) example shows AR staying flat through a restatement while MR moves — the definition of as-first-reported. Both dimensions ship, so full restatement lineage is reconstructable. [1][2]
- **Delisted is a headline feature, not an add-on.** ~10,000 delisted names carry the same dimensions and depth as live names; independently confirmed by QuantRocket ("Delisted: Yes"). [3][6][8] This is the strongest delisted evidence of any candidate.
- **Price clears the tiebreaker decisively** — **~$69/mo** for a personal full-history seat delivers full PIT dimensions + full delisted coverage, *cheaper than the current EODHD $99.99 tier while fixing the exact gap EODHD has*. [5]
- **Integration is LOW** against the existing EODHD keyed-REST/JSON pattern; the only new concept is the `dimension` param plus an SF1→DAILY join for price-based ratios. [1]

**One real cost risk, not a data risk:** the $69 price is the *personal/non-professional* seat. If Atlas's use is legally "professional activity," the pro full-history price is **quote-only** (`datesales@nasdaq.com`) and historically ran into the low hundreds/month. This is a licensing question for the Principal, not a defect in the data. [5]

### #2 — **Intrinio**, as the true-PIT alternative if the Sharadar license class blocks us.
Passes both disqualifiers (auditable as-reported XBRL, `filing_date` + restated-vs-reported flags; vendor-stated delisted coverage with counts). [12][13][14] Loses the tiebreaker on **price ($150/mo, ~2×)** and **integration (3–5 days, XBRL normalization layer)**. [16] Pick this only if a professional-license issue makes Sharadar uneconomic — it buys the strongest PIT *auditability* of the set.

### #3 — **Tiingo**, conditional on a delisted probe.
PIT design is documented and clean (`asReported` + release date), price is reasonable (~$80/mo ≈ EODHD), integration is the *easiest* of any true-PIT vendor. [9][10] **But its delisted axis is entirely UNVERIFIED** — the vendor supplies no verdict and the free tier (DOW-30) can't test it. On the disqualifier logic, an unproven delisted axis ranks it below Intrinio. If a one-month paid trial confirms the ~70 dead names, its price tiebreaker would pull it *ahead* of Intrinio. [9]

### #4 — **FMP**, fallback / pairing candidate only.
Cheap ($59/mo) and easy, but **partial on the primary disqualifier**: `acceptedDate` protects timing, yet the value fields are not restatement-safe (no vintage/as-of; standardized values can be silently overwritten). [17][18] Delisted *statement* coverage is unproven. [19] Use only if paired with a true-PIT source for the delisted sleeve — which defeats the point when Sharadar does both alone.

### #5 — **SimFin**, budget option, weakest guarantees.
Cheapest ($35/mo) and has a real `asreported` path, but default data is explicitly non-PIT, it exposes only a single Restated Date (no vintage chain), and delisted coverage is admittedly thinner. [23][25] Acceptable for a value/quality panel keyed on original-filing values, but the data guarantees are the softest of the paid set.

### #6 — **EODHD** (incumbent): **ruled OUT for fundamentals.**
Fails axis 1 outright (no PIT), and has a material delisted gap — **pre-2018 delistings carry no fundamentals at all**, poisoning roughly the first half of the 2012+ window. [28][30] **Retain EODHD as the price/calendar backbone and for historical index-constituent snapshots** (useful for building the delisted S&P 500 membership panel), but not as the fundamentals source. [31]

**Bottom line:** Approve **Sharadar SF1 at the ~$69 personal full-history seat**, pending a license-class confirmation. If that seat is unavailable to a fund entity, fall to **Intrinio ($150)**. Everything else is a compromise on a disqualifier axis.

---

## 3. What we still cannot verify without a trial subscription

Stated plainly — these are open items, not assumptions:

1. **Sharadar — professional-license full-history price.** The $69 figure is the personal/non-professional seat. Pro full-history monthly and all annual plans are **masked behind login/sales** — UNVERIFIED, expect materially higher. Whether a hypothetical paper fund legally requires the pro seat is a licensing determination. [5]
2. **Sharadar — the specific ~70 delisted S&P 500 names.** Coverage is "nearly" survivorship-free by the vendor's own wording; a handful of old/thinly-filed dead tickers may have gaps. Check against the **free TICKERS table** before committing. [8]
3. **Tiingo — delisted coverage, entirely.** No vendor verdict; free eval is DOW-30 only. **Cannot be confirmed without a paid month** ($49.99 add-on makes this cheap). This is the single biggest unknown for the #3 rank. [9]
4. **Tiingo — the PIT claim is documented but not independently audited**, and the underlying source is an unnamed "3rd-party provider." A trial diff of a known restatement (original 10-Q vs `asReported` output) is the verification step. [9]
5. **Intrinio — delisted fundamentals depth for the specific dead S&P names** (vs. its general "active & delisted" claim) is not itemized publicly; verify in the free trial. [14]
6. **FMP — delisted *statement-level* hit rate.** The go/no-go item. Probe `*-as-reported` for ~15–20 known-dead S&P 500 names across 2012–2024; measure non-empty hit rate and last-available period. [19] Also: exact monthly-billing prices (vs annual) and quantified restatement-overwrite risk. [22]
7. **SimFin — whether the `asreported` archive reaches 2012 at quarterly grain for delisted names**, and how many fields are non-null for dead tickers. The 20yr/2003 depth claim is for the *standard* (restated) dataset, not the as-reported variant. [23][25]
8. **Redistribution/commercial licensing across all five.** Atlas needs internal research only (no redistribution), which is the standard permitted use — but FMP, Tiingo, and Sharadar all maintain **separate commercial/professional tracks** a fund entity may be pushed toward. Confirm the seat class with each vendor's sales before relying on individual pricing.

None of these change the ranking; all are cheaply resolved on a one-month paid or free trial key before final commitment.

---

## 4. Next steps if the Principal approves a vendor

Assuming **Sharadar SF1** is approved (adjust vendor names if #2/#3 is chosen):

**A. Pre-purchase validation (before any adapter code, ~1 day)**
1. Pull the **free TICKERS table** and reconcile our ~70 delisted S&P 500 lineage list against it — confirm every name has SF1 rows (item 2 above). A miss here changes the decision.
2. Smoke-test the free sample: pull ARQ/ART for 5–10 delisted names back to 2012, confirm non-null value fields and `datekey` presence.
3. **Resolve the license class** with `datasales@nasdaq.com` — get the professional full-history number in writing so the Principal signs off on the *actual* cost, not the $69 personal quote.

**B. Adapter work (~few days, mirrors the EODHD adapter pattern)**
- New `SharadarAdapter` under `atlas/dcp/market_data/adapters/`, keyed REST against the Nasdaq Data Link Tables API (`GET .../datatables/SHARADAR/SF1.json?dimension=ARQ&...`), JSON payloads — same shape as the existing EODHD fetch loop. Golden-pin tests on a few known tickers per the working-style rule.
- New concept vs EODHD: the **`dimension` param** (select ARQ and/or ART for the PIT build) and the **SF1→DAILY (or SEP) join** for price-based ratios (marketcap/PE/PB numerators live in SF1; prices in DAILY).
- **Incremental nightly sync** via `lastupdated.gte=<last-run>` (SF1 refreshes 17:30/23:30 ET) — wire into the existing daily ingest job (`atlas/dcp/market_data/daily.py`).
- **New alembic migration (0033+)** for the PIT fundamentals tables — key vintages on `(ticker, datekey, dimension, reportperiod)`; never edit applied migrations.
- **Point-of-knowledge discipline (invariant #8, no look-ahead):** key every factor observation on `datekey` (the Form-10 filing date). Note this is conservative — it lags the 8-K earnings release by days; if factor timing needs the true disclosure moment, join the companion **EVENTS/8-K** table. Document the choice in the ADR.

**C. Factor-family design (the reason for the buy)**
- **Value:** E/P (EPS or netinc ÷ price×shares), B/P (BVPS/equity ÷ price), FCF yield (SF1 FCF ÷ marketcap from DAILY).
- **Quality:** GP/A (SF1 `gp` ÷ `assets`), margins (GROSSMARGIN/NETMARGIN/EBITDAMARGIN), accruals (balance-sheet + cash-flow line items).
- All directly computable from SF1 + DAILY; no agent numbers (invariant #2) — these are DCP-plane quant inputs.
- Backtest on the panel **including the ~70 delisted names**, under the existing quant gates: **register every trial** (`quant.trial_registry`), deflated Sharpe on the true trial count, null-model gate + purged walk-forward (invariant #7). Honest failures are deliverables — do not weaken a gate to make a value/quality sleeve pass.

**D. Reviewed catalog widening (governance)**
- Draft a signed **ADR** (next number in `docs/adr/`) recording: vendor choice, license class + actual price, PIT construction (AR* dims + `datekey`), delisted-panel validation result, and the `datekey`-vs-8-K timing decision. This is a load-bearing data-source change and follows Jay's signed-ADR pattern for anything that alters what the factor family is allowed to see.
- Update `README.md` phase checklists and the System Note artifact on completion.

**E. If Intrinio (#2) instead:** add ~2–3 days for the XBRL-tag normalization layer, and budget $150/mo. **If Tiingo (#3):** run step A's delisted probe as a **paid one-month trial first** — it is the gating unknown, and a thin result reverts the choice to Sharadar/Intrinio.

---

**Sources** (retrieved 2026-07):
[1] https://data.nasdaq.com/databases/SF1 · [2] https://data.nasdaq.com/databases/SF1/documentation · [3] https://data.nasdaq.com/api/v3/datatable_collections/SF1.json · [4] https://data.nasdaq.com/api/v3/datatables/SHARADAR/SF1/metadata.json · [5] https://data.nasdaq.com/api/v3/plans · [6] https://sharadar.com/ · [7] https://resources.quandl.com/a/res-hub/Sharadar_Datasheet_final.pdf · [8] https://www.quantrocket.com/sharadar/ · [9] https://www.tiingo.com/products/fundamental-data-api · [10] https://www.tiingo.com/documentation/fundamentals · [11] https://www.tiingo.com/about/pricing · [12] https://docs.intrinio.com/documentation/web_api/get_filing_fundamentals_v2 · [13] https://intrinio.com/blog/historical-financial-data-for-better-backtests-and-long-term-models · [14] https://intrinio.com/guides/starter-plan · [15] https://intrinio.com/products/us-fundamentals · [16] https://intrinio.com/pricing · [17] https://site.financialmodelingprep.com/developer/docs/stable/as-reported-financial-statements · [18] https://site.financialmodelingprep.com/faqs · [19] https://site.financialmodelingprep.com/developer/docs/stable/delisted-companies · [20] https://site.financialmodelingprep.com/how-to/how-to-handle-delisted-companies-and-historical-symbols-with-a-free-api · [21] https://site.financialmodelingprep.com/developer/docs/pricing · [22] https://site.financialmodelingprep.com/pricing-plans · [23] https://nbviewer.org/github/SimFin/simfin-tutorials/blob/master/01_Basics.ipynb · [24] https://simfin.readme.io/reference/statements-1.md · [25] https://www.simfin.com/en/simfin-screener-backtesting-tutorial/ · [26] https://simfin.com/forum/discussion/39/ · [27] https://www.simfin.com/en/prices/ · [28] https://eodhd.com/financial-apis/stock-etfs-fundamental-data-feeds · [29] https://eodhd.com/financial-apis/bulk-fundamentals-api · [30] https://eodhd.com/financial-apis/delisted-stock-companies-data · [31] https://eodhd.com/pricing