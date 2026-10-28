"""Factoken · tokenised invoice financing for SMEs (Streamlit MVP).

NEOMA MSc Fintech & DeFi · final group project.

Run it:
    pip install -r requirements.txt
    streamlit run app.py
(On Windows you can also double-click lancer_app.bat.)

The story: a small supplier (Atelier Morel) invoices a large buyer (Nordal
Aerospace) €50,000, payable in 60 days. Factoken turns the invoice into a unique
token. The buyer approves it, an investor buys it at a small discount and the
supplier gets the cash the next day. On the due date the buyer pays the token
holder through the contract. The same invoice can never be financed twice.

This MVP simulates three things (no real money, no real blockchain):
  * the chain: a local SHA-256 hash-chained ledger, one block per transaction;
  * the smart contract: a Python state machine with the rules planned for the
    Solidity version (unique invoice registry, buyer approval, payment routing);
  * EURC wallets (a MiCA-regulated euro stablecoin).
All companies are fictional.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime, time, timedelta

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Factoken · invoice financing", page_icon="🧾", layout="wide")

# ---------------------------------------------------------------------------
# 1. Settings and the rules the contract enforces
# ---------------------------------------------------------------------------
RISK_FREE = 2.50        # % a year: ECB deposit facility rate since 16 Sept 2026 (investor benchmark)
PENALTY_RATE = 12.40    # % a year: ECB rate on 1 July 2026 (2.40 %) + 10 points, default late-payment
                        # rate for the second half of 2026 (Code de commerce, art. L441-10)
RECOVERY_FEE = 40.0     # € fixed indemnity per late invoice (Code de commerce, art. L441-10 and D441-5)
MAX_TERM_DAYS = 60      # payment term cap: 60 days after the invoice date (art. L441-10)
PLATFORM_FEE = 0.30     # % of the invoice amount, paid by the supplier when the invoice is funded
SPREADS = {"A": 1.5, "BBB": 2.5, "BB": 4.0}   # credit spread over the risk-free rate (assumption)
TODAY = date(2026, 10, 2)                      # the demo's "today"
TOKEN = "EURC"

DEMO = {
    "supplier": "Atelier Morel SAS",
    "buyer": "Nordal Aerospace",
    "rating": "A",
    "number": "F-2026-0142",
    "amount": 50000.0,
    "issued": date(2026, 10, 1),
    "due": date(2026, 11, 30),
}
MARKET = [  # invoices from other suppliers, already approved by their buyers
    {"supplier": "Imprimerie Lambert", "buyer": "Helio Retail", "rating": "BBB",
     "number": "IL-2026-0877", "amount": 12000.0, "issued": date(2026, 9, 20), "due": date(2026, 11, 4)},
    {"supplier": "Logis Transports", "buyer": "Strata Construction", "rating": "BB",
     "number": "LT-26-3310", "amount": 30000.0, "issued": date(2026, 9, 28), "due": date(2026, 10, 28)},
    {"supplier": "Labo Arden", "buyer": "Medivo Pharma", "rating": "A",
     "number": "LA-2026-041", "amount": 20000.0, "issued": date(2026, 10, 1), "due": date(2026, 11, 30)},
]

STAGES = {
    1: "Fresh start",
    2: "Invoice tokenised and approved",
    3: "Invoice funded, portfolio built",
    4: "Paid on the due date, closed",
}
STATUS_LABEL = {
    "NONE": "No invoice yet",
    "ISSUED": "Tokenised, waiting for the buyer",
    "APPROVED": "Approved, ready to be funded",
    "DISPUTED": "Disputed by the buyer",
    "FUNDED": "Funded, waiting for the due date",
    "PAID": "Paid and closed",
}
ORDER = ["NONE", "ISSUED", "APPROVED", "FUNDED", "PAID"]
STEPS = [("1 · Invoice tokenised", 1), ("2 · Buyer approval", 2), ("3 · Funded", 3), ("4 · Paid", 4)]


# ---------------------------------------------------------------------------
# 2. Small helpers
# ---------------------------------------------------------------------------
def sha256_of(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def address_of(label: str) -> str:
    """Deterministic fake wallet address for a company or a contract."""
    return "0x" + hashlib.sha256(label.strip().lower().encode("utf-8")).hexdigest()[:40]


CONTRACT = address_of("Factoken invoice registry v1")
TREASURY = address_of("Factoken treasury")
INVESTOR = address_of("Vesle Capital")
RIVAL = address_of("Banque Rivage")


def short(value: str) -> str:
    value = str(value)
    if not value.startswith("0x"):
        value = "0x" + value
    return value[:8] + "…" + value[-4:]


def eur(x: float) -> str:
    return f"€{x:,.2f}"


def pct(x: float) -> str:
    return f"{x:.2f} %"


def day(d) -> str:
    return d.strftime("%d %b %Y")


def norm(text: str) -> str:
    return " ".join(str(text).split()).lower()


def fingerprint(supplier, buyer, number, amount, issued) -> str:
    return sha256_of([norm(supplier), norm(buyer), norm(number).upper(), round(float(amount), 2),
                      str(issued)])


def funding_date(inv: dict) -> date:
    return max(TODAY, inv["issued"] + timedelta(days=1))


def quote(inv: dict) -> dict:
    """Price of the invoice if an investor buys it now (French discount, 360-day basis)."""
    on = funding_date(inv)
    days = max((inv["due"] - on).days, 1)
    rate = RISK_FREE + SPREADS[inv["rating"]]
    discount = round(inv["amount"] * rate / 100 * days / 360, 2)
    price = round(inv["amount"] - discount, 2)
    fee = round(inv["amount"] * PLATFORM_FEE / 100, 2)
    proceeds = round(price - fee, 2)
    return {
        "on": on, "days": days, "rate": rate, "discount": discount, "price": price, "fee": fee,
        "proceeds": proceeds,
        "investor_yield": (inv["amount"] / price - 1) * 365 / days * 100,
        "supplier_cost": (inv["amount"] - proceeds) / inv["amount"] * 100,
    }


def late_penalty(amount: float, late_days: int) -> float:
    if late_days <= 0:
        return 0.0
    return round(amount * PENALTY_RATE / 100 * late_days / 365 + RECOVERY_FEE, 2)


# ---------------------------------------------------------------------------
# 3. The chain: one block per transaction, each block hashes the previous one
# ---------------------------------------------------------------------------
BLOCK_FIELDS = ("index", "time", "event", "signer", "status", "data", "prev_hash")


def block_hash(block: dict) -> str:
    return sha256_of({key: block[key] for key in BLOCK_FIELDS})


def add_block(s: dict, event: str, signer: str, data: dict, when: datetime,
              status: str = "success") -> dict:
    chain = s["chain"]
    if chain:  # time only moves forward
        when = max(when, datetime.strptime(chain[-1]["time"], "%Y-%m-%d %H:%M") + timedelta(minutes=1))
    block = {
        "index": len(chain),
        "time": when.strftime("%Y-%m-%d %H:%M"),
        "event": event,
        "signer": signer,
        "status": status,
        "data": copy.deepcopy(data),
        "prev_hash": chain[-1]["hash"] if chain else "0" * 64,
    }
    block["hash"] = block_hash(block)
    chain.append(block)
    return block


def verify_chain(chain: list) -> tuple:
    for i, block in enumerate(chain):
        if block_hash(block) != block["hash"]:
            return False, i, "its content no longer matches its hash"
        if i and block["prev_hash"] != chain[i - 1]["hash"]:
            return False, i, "it no longer points to the previous block"
    return True, None, ""


def last_time(s: dict) -> datetime:
    return datetime.strptime(s["chain"][-1]["time"], "%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 4. The smart contract: a registry of invoice tokens
# ---------------------------------------------------------------------------
class ContractError(Exception):
    """The contract refused the transaction (it reverts)."""


def register_name(s: dict, name: str, role: str) -> str:
    addr = address_of(name)
    s["names"].setdefault(addr, f"{name.strip()} ({role})")
    return addr


def new_state() -> dict:
    s = {
        "invoices": {},
        "registry": {},     # fingerprint -> token id
        "numbers": {},      # supplier + invoice number -> token id
        "wallets": {},
        "names": {
            CONTRACT: "Factoken contract",
            TREASURY: "Factoken treasury",
            INVESTOR: "Vesle Capital (investor)",
            RIVAL: "Banque Rivage (bank)",
            "system": "Network",
        },
        "chain": [],
        "demo": None,
        "tamper": None,
    }
    add_block(s, "GENESIS", "system",
              {"network": "Factoken devnet (simulated EVM chain)", "token": TOKEN},
              datetime(2026, 1, 1))
    seeded = []
    for m in MARKET:
        inv = mint(s, m)
        inv["status"] = "APPROVED"
        inv["approved_on"] = m["issued"] + timedelta(days=2)
        seeded.append({"token": inv["id"], "invoice": m["number"], "supplier": m["supplier"],
                       "buyer": m["buyer"], "amount_eur": m["amount"], "due": str(m["due"]),
                       "fingerprint": "0x" + inv["fingerprint"]})
    add_block(s, "MARKETPLACE_OPENED", CONTRACT,
              {"note": "Invoices tokenised and approved by their buyers earlier", "invoices": seeded},
              datetime(2026, 9, 30, 18, 0))
    s["wallets"] = {
        address_of(DEMO["buyer"]): 1_000_000.0,
        INVESTOR: 250_000.0,
        RIVAL: 500_000.0,
        TREASURY: 0.0,
    }
    return s


def mint(s: dict, d: dict) -> dict:
    """Create the token (no checks: the checks live in tokenise)."""
    token_id = f"INV-{len(s['invoices']) + 1}"
    supplier_addr = register_name(s, d["supplier"], "supplier")
    buyer_addr = register_name(s, d["buyer"], "buyer")
    fp = fingerprint(d["supplier"], d["buyer"], d["number"], d["amount"], d["issued"])
    inv = {
        "id": token_id, "number": d["number"].strip(), "supplier": d["supplier"].strip(),
        "buyer": d["buyer"].strip(), "rating": d["rating"], "amount": round(float(d["amount"]), 2),
        "issued": d["issued"], "due": d["due"], "supplier_addr": supplier_addr,
        "buyer_addr": buyer_addr, "holder": supplier_addr, "fingerprint": fp, "status": "ISSUED",
        "approved_on": None, "funded": None, "paid": None,
    }
    s["invoices"][token_id] = inv
    s["registry"][fp] = token_id
    s["numbers"][norm(d["supplier"]) + "|" + norm(d["number"])] = token_id
    return inv


def need(inv: dict, *statuses: str) -> None:
    if inv["status"] not in statuses:
        raise ContractError(f"Not possible now: the invoice is “{STATUS_LABEL[inv['status']]}”.")


def holder_name(s: dict, inv: dict) -> str:
    return s["names"].get(inv["holder"], short(inv["holder"]))


def tokenise(s, supplier, buyer, rating, number, amount, issued, due) -> None:
    supplier, buyer, number = supplier.strip(), buyer.strip(), number.strip()
    if not supplier or not buyer or not number:
        raise ContractError("Fill in the supplier, the buyer and the invoice number.")
    if norm(supplier) == norm(buyer):
        raise ContractError("The supplier and the buyer must be two different companies.")
    if amount <= 0:
        raise ContractError("The invoice amount must be above zero.")
    if due <= issued:
        raise ContractError("The due date must come after the invoice date.")
    when = datetime.combine(issued, time(16, 0))
    term = (due - issued).days
    signer = address_of(supplier)
    if term > MAX_TERM_DAYS:
        add_block(s, "TOKENISE_INVOICE", signer,
                  {"invoice": number, "payment_term_days": term,
                   "revert_reason": "Payment term above the 60-day legal cap (L441-10)"},
                  when, status="reverted")
        raise ContractError(f"Transaction reverted: a {term}-day payment term is above the legal "
                            f"cap of {MAX_TERM_DAYS} days after the invoice date (Code de commerce, "
                            "art. L441-10).")
    fp = fingerprint(supplier, buyer, number, amount, issued)
    existing = s["registry"].get(fp) or s["numbers"].get(norm(supplier) + "|" + norm(number))
    if existing:
        raise_duplicate(s, s["invoices"][existing], signer, when)
    if due <= max(TODAY, issued + timedelta(days=1)):
        raise ContractError(f"This invoice is due on {day(due)}: there is nothing left to finance. "
                            f"Pick a due date after {day(max(TODAY, issued + timedelta(days=1)))}.")
    d = {"supplier": supplier, "buyer": buyer, "rating": rating, "number": number,
         "amount": amount, "issued": issued, "due": due}
    inv = mint(s, d)
    s["demo"] = inv["id"]
    add_block(s, "INVOICE_TOKENISED", inv["supplier_addr"], {
        "token": inv["id"], "invoice": inv["number"], "supplier": inv["supplier_addr"],
        "buyer": inv["buyer_addr"], "amount_eur": inv["amount"], "invoice_date": str(issued),
        "due_date": str(due), "payment_term_days": term,
        "fingerprint": "0x" + inv["fingerprint"], "holder": inv["supplier_addr"],
    }, when)


def raise_duplicate(s, inv, signer, when, financier=None) -> None:
    data = {"invoice": inv["number"], "fingerprint": "0x" + inv["fingerprint"],
            "revert_reason": f"Invoice already tokenised as {inv['id']}, held by {holder_name(s, inv)}"}
    if financier:
        data["financier"] = financier
    add_block(s, "TOKENISE_INVOICE", signer, data, when, status="reverted")
    raise ContractError(
        f"Transaction reverted: invoice {inv['number']} is already tokenised ({inv['id']}) and held by "
        f"{holder_name(s, inv)}. The same invoice can never be financed twice.")


def double_finance(s, token_id) -> None:
    """Demo: the supplier tries to get the same invoice financed again by its bank."""
    inv = s["invoices"][token_id]
    raise_duplicate(s, inv, inv["supplier_addr"], last_time(s) + timedelta(hours=2),
                    financier="Banque Rivage")


def approve(s, token_id) -> None:
    inv = s["invoices"][token_id]
    need(inv, "ISSUED")
    inv["status"] = "APPROVED"
    inv["approved_on"] = funding_date(inv)
    add_block(s, "BUYER_APPROVED", inv["buyer_addr"], {
        "token": token_id, "invoice": inv["number"], "amount_eur": inv["amount"],
        "due_date": str(inv["due"]), "pay_to": CONTRACT,
        "notice": "Buyer confirms delivery and amount, and will pay whoever holds the token",
    }, datetime.combine(inv["approved_on"], time(9, 0)))


def dispute(s, token_id) -> None:
    inv = s["invoices"][token_id]
    need(inv, "ISSUED")
    inv["status"] = "DISPUTED"
    add_block(s, "BUYER_DISPUTED", inv["buyer_addr"], {
        "token": token_id, "invoice": inv["number"],
        "reason": "Delivery or amount contested: the invoice cannot be financed",
    }, datetime.combine(funding_date(inv), time(9, 0)))


def fund(s, token_id, investor=INVESTOR) -> None:
    inv = s["invoices"][token_id]
    when = datetime.combine(funding_date(inv), time(10, 15))
    if inv["status"] in ("ISSUED", "DISPUTED"):
        add_block(s, "FUND_INVOICE", investor, {
            "token": token_id, "revert_reason": "Only invoices approved by the buyer can be funded"},
            when, status="reverted")
        raise ContractError("Transaction reverted: only invoices approved by the buyer can be funded.")
    need(inv, "APPROVED")
    q = quote(inv)
    if inv["due"] <= q["on"]:
        raise ContractError("This invoice is already due: it can no longer be funded.")
    if s["wallets"].get(investor, 0.0) < q["price"]:
        raise ContractError("The investor's wallet does not hold enough EURC.")
    s["wallets"][investor] = round(s["wallets"][investor] - q["price"], 2)
    s["wallets"][inv["supplier_addr"]] = round(s["wallets"].get(inv["supplier_addr"], 0.0) + q["proceeds"], 2)
    s["wallets"][TREASURY] = round(s["wallets"].get(TREASURY, 0.0) + q["fee"], 2)
    previous = inv["holder"]
    inv["holder"] = investor
    inv["status"] = "FUNDED"
    inv["funded"] = q
    add_block(s, "INVOICE_FUNDED", investor, {
        "token": token_id, "price_eurc": q["price"], "to_supplier_eurc": q["proceeds"],
        "factoken_fee_eurc": q["fee"], "days_to_due": q["days"], "discount_rate_pct": q["rate"],
        "investor_yield_pct": round(q["investor_yield"], 2),
        "holder_from": previous, "holder_to": investor,
    }, when)


def pay(s, token_id, late_days: int = 0) -> None:
    inv = s["invoices"][token_id]
    need(inv, "FUNDED")
    late_days = max(int(late_days), 0)
    paid_on = inv["due"] + timedelta(days=late_days)
    penalty = late_penalty(inv["amount"], late_days)
    total = round(inv["amount"] + penalty, 2)
    buyer = inv["buyer_addr"]
    s["wallets"][buyer] = round(s["wallets"].get(buyer, 1_000_000.0) - total, 2)
    holder = inv["holder"]
    s["wallets"][holder] = round(s["wallets"].get(holder, 0.0) + total, 2)
    q = inv["funded"]
    held_days = max((paid_on - q["on"]).days, 1)
    profit = round(total - q["price"], 2)
    inv["paid"] = {"on": paid_on, "late_days": late_days, "penalty": penalty, "total": total,
                   "to": holder, "profit": profit,
                   "realised_yield": profit / q["price"] * 365 / held_days * 100}
    inv["status"] = "PAID"
    when = datetime.combine(paid_on, time(11, 0))
    add_block(s, "BUYER_PAID", buyer, {
        "token": token_id, "amount_eurc": inv["amount"], "late_days": late_days,
        "late_penalty_eurc": penalty, "penalty_rule": f"{PENALTY_RATE} % a year + €{RECOVERY_FEE:.0f} (L441-10)",
        "paid_to": holder,
    }, when)
    add_block(s, "TOKEN_BURNED", CONTRACT, {
        "token": token_id, "invoice": inv["number"], "settled_total_eurc": total,
        "investor_profit_eurc": profit,
    }, when + timedelta(minutes=1))


def tamper_invoice(s) -> None:
    """Demo: someone rewrites the amount of the tokenised invoice after the fact."""
    block = next((b for b in s["chain"] if b["event"] == "INVOICE_TOKENISED"), None)
    if block is None:
        raise ContractError("Tokenise the invoice first: the tamper test rewrites it.")
    if s["tamper"]:
        return
    s["tamper"] = {"index": block["index"], "data": copy.deepcopy(block["data"])}
    block["data"]["amount_eur"] = 80000.0


def restore_invoice(s) -> None:
    if s["tamper"]:
        s["chain"][s["tamper"]["index"]]["data"] = s["tamper"]["data"]
        s["tamper"] = None


# ---------------------------------------------------------------------------
# 5. Session state, demo stages and callbacks
# ---------------------------------------------------------------------------
def reset_demo() -> None:
    st.session_state.s = new_state()
    st.session_state.ver = st.session_state.get("ver", 0) + 1
    st.session_state.flash = ("success", "Demo reset: a fresh chain with the marketplace only.")


def load_stage(stage: int) -> None:
    reset_demo()
    s = st.session_state.s
    if stage >= 2:
        tokenise(s, DEMO["supplier"], DEMO["buyer"], DEMO["rating"], DEMO["number"],
                 DEMO["amount"], DEMO["issued"], DEMO["due"])
        approve(s, s["demo"])
    if stage >= 3:
        fund(s, s["demo"])
        for token_id in ("INV-1", "INV-2", "INV-3"):
            fund(s, token_id)
    if stage >= 4:
        pay(s, s["demo"], 0)
    st.session_state.flash = ("success", f"Demo stage loaded: {STAGES[stage]}.")


def run(action, *args, ok: str = "") -> None:
    """Send a transaction to the contract, then refresh the page."""
    try:
        action(*args)
    except ContractError as error:
        st.session_state.flash = ("error", str(error))
    else:
        st.session_state.flash = ("success", ok) if ok else None
    st.rerun()


if "s" not in st.session_state:
    st.session_state.s = new_state()
    st.session_state.ver = 0

# Optional: open the app straight at a stage, e.g. http://localhost:8501/?stage=3
if not st.session_state.get("query_stage_done"):
    st.session_state.query_stage_done = True
    wanted = st.query_params.get("stage")
    if wanted in ("1", "2", "3", "4"):
        load_stage(int(wanted))

s = st.session_state.s
ver = st.session_state.ver
demo = s["invoices"].get(s["demo"]) if s["demo"] else None
status = demo["status"] if demo else "NONE"


# ---------------------------------------------------------------------------
# 6. Page
# ---------------------------------------------------------------------------
st.markdown("""
<style>
.fk-hero {padding: 1.1rem 1.4rem; border-radius: 16px; margin-bottom: .6rem;
  background: linear-gradient(120deg, #0F1F3D 0%, #1C3A66 60%, #2B4F80 100%); color: #fff;}
.fk-hero .fk-title {font-size: 2rem; font-weight: 800; letter-spacing: -.02em; margin: 0;}
.fk-hero .fk-sub {color: #D9E2F0; margin: .15rem 0 0 0; font-size: 1.02rem;}
.fk-steps {display: flex; flex-wrap: wrap; gap: .4rem; margin: .4rem 0 .9rem 0;}
.fk-step {padding: .22rem .75rem; border-radius: 999px; font-size: .85rem;
  border: 1px solid rgba(128,128,128,.4); opacity: .6;}
.fk-step.done {background: rgba(217,154,30,.16); border-color: rgba(217,154,30,.7); opacity: 1;}
.fk-step.now {background: #D99A1E; border-color: #D99A1E; color: #1B1405; opacity: 1; font-weight: 700;}
.fk-step.bad {background: rgba(200,60,60,.15); border-color: rgba(200,60,60,.7); opacity: 1;}
.fk-mono {font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .85rem;}
</style>
""", unsafe_allow_html=True)

st.markdown(
    "<div class='fk-hero'><p class='fk-title'>🧾 Factoken</p>"
    "<p class='fk-sub'>Get paid in 1 day, not 60. Tokenised invoices that can never be financed twice.</p></div>",
    unsafe_allow_html=True,
)

flash = st.session_state.get("flash")
if flash:
    kind, message = flash
    if kind == "error":
        st.error(message, icon="⛔")
    else:
        st.success(message, icon="✅")
    st.session_state.flash = None

position = ORDER.index(status) if status in ORDER else 1
chips = []
current_marked = False
for label, done_at in STEPS:
    if status == "DISPUTED" and done_at == 2:
        chips.append(f"<span class='fk-step bad'>✗ {label}: disputed</span>")
        current_marked = True
    elif position >= done_at:
        chips.append(f"<span class='fk-step done'>✓ {label}</span>")
    elif not current_marked:
        chips.append(f"<span class='fk-step now'>{label}</span>")
        current_marked = True
    else:
        chips.append(f"<span class='fk-step'>{label}</span>")
st.markdown("<div class='fk-steps'>" + "".join(chips) + "</div>", unsafe_allow_html=True)

funded = demo["funded"] if demo and demo["funded"] else None
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Invoice", eur(demo["amount"]) if demo else "—",
          f"due {day(demo['due'])}" if demo else None, delta_color="off")
k2.metric("Cash to the supplier", eur(funded["proceeds"]) if funded else eur(0))
k3.metric("Days early", f"{funded['days']} days" if funded else "—",
          help="How long before the due date the supplier gets the cash.")
k4.metric("Investor yield", pct(funded["investor_yield"]) if funded else "—",
          f"{funded['investor_yield'] - RISK_FREE:+.2f} pts over risk-free" if funded else None,
          delta_color="off")
k5.metric("Blocks on chain", len(s["chain"]))

# ---- Sidebar: demo controls and wallets ------------------------------------
with st.sidebar:
    st.markdown("### Demo controls")
    st.caption("Simulated chain and wallets: no real money moves. All companies are fictional.")
    stage = st.selectbox("Jump to a demo stage", list(STAGES),
                         format_func=lambda k: f"{k} · {STAGES[k]}", key=f"stage_pick_{ver}")
    st.button("Load this stage", on_click=load_stage, args=(stage,))
    st.caption(f"Demo date: {day(TODAY)} · risk-free rate {pct(RISK_FREE)} (ECB deposit rate)")
    st.divider()
    st.markdown("### Wallets (EURC)")
    supplier_addr = address_of(DEMO["supplier"]) if not demo else demo["supplier_addr"]
    buyer_addr = address_of(DEMO["buyer"]) if not demo else demo["buyer_addr"]
    rows = [
        ((demo["supplier"] if demo else DEMO["supplier"]) + " · supplier", supplier_addr,
         s["wallets"].get(supplier_addr, 0.0)),
        ((demo["buyer"] if demo else DEMO["buyer"]) + " · buyer", buyer_addr,
         s["wallets"].get(buyer_addr, 1_000_000.0)),
        ("Vesle Capital · investor", INVESTOR, s["wallets"].get(INVESTOR, 0.0)),
        ("Banque Rivage · bank", RIVAL, s["wallets"].get(RIVAL, 0.0)),
        ("Factoken treasury", TREASURY, s["wallets"].get(TREASURY, 0.0)),
    ]
    for name, addr, balance in rows:
        st.markdown(f"**{name}**  \n<span class='fk-mono'>{short(addr)}</span> · {eur(balance)}",
                    unsafe_allow_html=True)
    st.divider()
    st.button("Reset demo", on_click=reset_demo)

tab_inv, tab_buyer, tab_fund, tab_pay, tab_port, tab_chain, tab_rules = st.tabs([
    "① Invoice", "② Buyer approval", "③ Financing", "④ Payment", "⑤ Investor portfolio",
    "⑥ Ledger explorer", "Rules"])

# ---- Tab 1: the supplier tokenises the invoice -------------------------------
with tab_inv:
    if not demo:
        st.subheader("Supplier: tokenise an invoice")
        st.caption("Atelier Morel, a 38-person metal-parts maker in Reims, delivered parts to Nordal "
                   "Aerospace. Its invoice becomes a unique token: the fingerprint is registered "
                   "on-chain, so the invoice can never be financed twice.")
        c1, c2, c3 = st.columns([2, 2, 1])
        supplier = c1.text_input("Supplier (the SME)", value=DEMO["supplier"], key=f"sup_{ver}")
        buyer = c2.text_input("Buyer (the client)", value=DEMO["buyer"], key=f"buy_{ver}")
        rating = c3.selectbox("Buyer rating", list(SPREADS), key=f"rat_{ver}",
                              help="Credit quality of the buyer. A weaker buyer means a higher discount.")
        c4, c5, c6, c7 = st.columns(4)
        number = c4.text_input("Invoice number", value=DEMO["number"], key=f"num_{ver}")
        amount = c5.number_input("Amount incl. VAT (€)", min_value=0.0, value=DEMO["amount"],
                                 step=1000.0, key=f"amt_{ver}")
        issued = c6.date_input("Invoice date", value=DEMO["issued"], key=f"iss_{ver}")
        due = c7.date_input("Due date", value=DEMO["due"], key=f"due_{ver}")
        term = (due - issued).days
        if term > MAX_TERM_DAYS:
            st.warning(f"Payment term: {term} days. That is above the legal cap of {MAX_TERM_DAYS} days "
                       "after the invoice date (Code de commerce, art. L441-10): the contract will refuse it.")
        else:
            st.info(f"Payment term: {term} days. Legal cap: {MAX_TERM_DAYS} days after the invoice date "
                    "(Code de commerce, art. L441-10).")
        if st.button("🧾 Supplier: tokenise the invoice", type="primary"):
            run(tokenise, s, supplier, buyer, rating, number, amount, issued, due,
                ok="Invoice tokenised: its fingerprint is now on-chain. Next step: the buyer approves it (tab ②).")
    else:
        left, right = st.columns([3, 2])
        with left:
            with st.container(border=True):
                st.markdown(f"#### 🧾 Invoice {demo['number']} · token {demo['id']}")
                st.markdown(
                    f"**Supplier:** {demo['supplier']} <span class='fk-mono'>{short(demo['supplier_addr'])}</span>  \n"
                    f"**Buyer:** {demo['buyer']} (rating {demo['rating']}) "
                    f"<span class='fk-mono'>{short(demo['buyer_addr'])}</span>  \n"
                    f"**Amount:** {eur(demo['amount'])} · **Invoice date:** {day(demo['issued'])} · "
                    f"**Due:** {day(demo['due'])} ({(demo['due'] - demo['issued']).days} days)",
                    unsafe_allow_html=True)
        with right:
            with st.container(border=True):
                st.markdown("#### 🔐 On-chain token")
                st.markdown(
                    f"**Fingerprint:** <span class='fk-mono'>{short(demo['fingerprint'])}</span>  \n"
                    f"**Held by:** {holder_name(s, demo)}  \n"
                    f"**Status:** {STATUS_LABEL[demo['status']]}",
                    unsafe_allow_html=True)
        st.divider()
        st.markdown("**Security test**: the supplier tries to get the same invoice financed a second "
                    "time by its bank. This is the risk investigated in the First Brands collapse (2025).")
        if st.button(f"🏦 Banque Rivage: finance invoice {demo['number']} again"):
            run(double_finance, s, demo["id"])

# ---- Tab 2: the buyer approves -----------------------------------------------
with tab_buyer:
    if not demo:
        st.info("Tokenise the invoice first (tab ①).")
    elif status == "ISSUED":
        st.subheader(f"Buyer: {demo['buyer']} checks the invoice")
        st.caption("By approving, the buyer confirms the delivery and the amount. It also commits to "
                   "pay whoever holds the token, through the contract, and no longer the supplier's "
                   "bank account. That is what makes the invoice safe to finance.")
        b1, b2 = st.columns(2)
        if b1.button("✅ Buyer: approve the invoice", type="primary"):
            run(approve, s, demo["id"],
                ok="Invoice approved: it can now be funded (tab ③).")
        if b2.button("✋ Buyer: dispute the invoice"):
            run(dispute, s, demo["id"], ok="Invoice disputed: it can no longer be financed.")
    elif status == "DISPUTED":
        st.warning("The buyer disputed this invoice, so nobody can finance it. "
                   "Load a demo stage or reset (sidebar) to try again.")
    else:
        st.success(f"Approved by {demo['buyer']} on {day(demo['approved_on'])}. Payment is locked to "
                   f"the contract {short(CONTRACT)}, which pays the token holder.")

# ---- Tab 3: financing ----------------------------------------------------------
with tab_fund:
    if not demo:
        st.info("Tokenise the invoice first (tab ①).")
    else:
        q = demo["funded"] or quote(demo)
        st.subheader(f"Invoice {demo['number']}: the offer")
        left, right = st.columns(2)
        with left:
            with st.container(border=True):
                st.markdown(f"#### 🏭 {demo['supplier']} (supplier)")
                st.markdown(
                    f"Gets **{eur(q['proceeds'])}** on {day(q['on'])} instead of {eur(demo['amount'])} "
                    f"on {day(demo['due'])}: **{q['days']} days earlier**.  \n"
                    f"Total cost: {eur(demo['amount'] - q['proceeds'])}, i.e. {q['supplier_cost']:.2f} % "
                    f"of the invoice (discount {eur(q['discount'])} + Factoken fee {eur(q['fee'])}).")
        with right:
            with st.container(border=True):
                st.markdown("#### 💼 Vesle Capital (investor)")
                st.markdown(
                    f"Pays **{eur(q['price'])}** and receives {eur(demo['amount'])} on {day(demo['due'])}.  \n"
                    f"Return: **{pct(q['investor_yield'])} a year**, i.e. "
                    f"{q['investor_yield'] - RISK_FREE:+.2f} pts above the risk-free rate ({pct(RISK_FREE)}). "
                    f"Discount rate: {pct(q['rate'])} ({pct(RISK_FREE)} + {SPREADS[demo['rating']]:.1f} pts "
                    f"for a buyer rated {demo['rating']}).")
        if status == "APPROVED":
            if st.button(f"💶 Investor: buy the invoice for {eur(q['price'])}", type="primary"):
                run(fund, s, demo["id"],
                    ok=f"Funded: {demo['supplier']} received {eur(q['proceeds'])} today. "
                       "The token now belongs to Vesle Capital.")
        elif status in ("ISSUED", "DISPUTED"):
            st.info("The buyer has not approved this invoice: it cannot be funded.")
            if st.button("Investor: try to buy it anyway"):
                run(fund, s, demo["id"])
        else:
            st.success(f"Funded on {day(q['on'])}: {demo['supplier']} received {eur(q['proceeds'])}, "
                       f"{q['days']} days before the due date.")

    st.divider()
    st.subheader("Marketplace: other approved invoices")
    st.caption("Invoices from other suppliers, already approved by their buyers. Buying several "
               "spreads the investor's risk across buyers (see tab ⑤).")
    market = [inv for inv in s["invoices"].values() if inv["id"] != s["demo"]]
    for inv in market:
        mq = inv["funded"] or quote(inv)
        with st.container(border=True):
            a, b, c, d = st.columns([3, 2, 2, 2])
            a.markdown(f"**{inv['id']} · {inv['supplier']}** → {inv['buyer']} (rating {inv['rating']})  \n"
                       f"Invoice {inv['number']} · due {day(inv['due'])}")
            b.markdown(f"**{eur(inv['amount'])}**  \n{mq['days']} days to go")
            c.markdown(f"Price **{eur(mq['price'])}**  \n{pct(mq['investor_yield'])} a year")
            if inv["status"] == "APPROVED":
                if d.button(f"Buy {inv['id']}", key=f"buy_{inv['id']}_{ver}"):
                    run(fund, s, inv["id"], ok=f"{inv['id']} bought by Vesle Capital.")
            else:
                d.markdown(f"✅ {STATUS_LABEL[inv['status']]}")

# ---- Tab 4: payment ------------------------------------------------------------
with tab_pay:
    if not demo or status in ("ISSUED", "APPROVED", "DISPUTED"):
        st.info("This step opens once the invoice is funded (tab ③).")
    elif status == "FUNDED":
        st.subheader(f"Due date: {day(demo['due'])}")
        st.caption(f"{demo['buyer']} pays the contract, which forwards the money to the token holder: "
                   f"{holder_name(s, demo)}. If the buyer pays late, the legal penalties "
                   f"({PENALTY_RATE} % a year + €{RECOVERY_FEE:.0f}) are added automatically.")
        late_key = f"late_{ver}"
        if late_key not in st.session_state:
            st.session_state[late_key] = 15
        p1, p2 = st.columns(2)
        with p1:
            if st.button("📅 Buyer pays on the due date", type="primary"):
                run(pay, s, demo["id"], 0,
                    ok="Paid on time: the investor got the full amount and the token was burned.")
        with p2:
            late = st.slider("Days late", min_value=1, max_value=60, step=1, key=late_key)
            penalty = late_penalty(demo["amount"], late)
            st.caption(f"Penalty: {eur(demo['amount'])} × {PENALTY_RATE} % × {late}/365 + "
                       f"€{RECOVERY_FEE:.0f} = **{eur(penalty)}**")
            if st.button(f"⏰ Buyer pays {late} days late"):
                run(pay, s, demo["id"], late,
                    ok=f"Paid {late} days late: the contract added {eur(penalty)} of legal penalties.")
    else:
        paid, q = demo["paid"], demo["funded"]
        st.success(f"Closed on {day(paid['on'])}: {demo['buyer']} paid {eur(paid['total'])} to "
                   f"{s['names'].get(paid['to'], short(paid['to']))}. The token was burned.")
        a, b, c, d = st.columns(4)
        a.metric("Supplier got", eur(q["proceeds"]), f"{q['days']} days early", delta_color="off")
        b.metric("Investor profit", eur(paid["profit"]),
                 f"{pct(paid['realised_yield'])} a year", delta_color="off")
        c.metric("Late penalties", eur(paid["penalty"]),
                 f"{paid['late_days']} days late" if paid["late_days"] else "paid on time",
                 delta_color="off")
        d.metric("Factoken fee", eur(q["fee"]))
        st.dataframe(pd.DataFrame([
            {"Who": demo["supplier"], "Role": "Supplier", "Pays": "—", "Receives": eur(q["proceeds"]),
             "When": day(q["on"])},
            {"Who": "Vesle Capital", "Role": "Investor", "Pays": eur(q["price"]),
             "Receives": eur(paid["total"]), "When": day(paid["on"])},
            {"Who": demo["buyer"], "Role": "Buyer", "Pays": eur(paid["total"]), "Receives": "—",
             "When": day(paid["on"])},
            {"Who": "Factoken", "Role": "Platform", "Pays": "—", "Receives": eur(q["fee"]),
             "When": day(q["on"])},
        ]), hide_index=True)

# ---- Tab 5: investor portfolio -------------------------------------------------
with tab_port:
    held = [inv for inv in s["invoices"].values()
            if inv["funded"] and (inv["holder"] == INVESTOR or (inv["paid"] and inv["paid"]["to"] == INVESTOR))]
    if not held:
        st.info("Vesle Capital holds no invoices yet. Buy one in tab ③.")
    else:
        st.subheader("Vesle Capital's invoice portfolio")
        st.caption("Same ideas as a stock portfolio: the annualised return of each asset, compared with "
                   "the risk-free rate, and diversification so that one buyer paying late or "
                   "defaulting does not sink the whole portfolio.")
        table = []
        for inv in held:
            q = inv["funded"]
            if inv["paid"]:
                receive, ann, state = inv["paid"]["total"], inv["paid"]["realised_yield"], "Paid"
            else:
                receive, ann, state = inv["amount"], q["investor_yield"], "Open"
            table.append({
                "Token": inv["id"], "Buyer": inv["buyer"], "Rating": inv["rating"],
                "Paid (€)": q["price"], "Receives (€)": receive, "Days": q["days"],
                "Yield a year (%)": round(ann, 2), "Spread vs risk-free (pts)": round(ann - RISK_FREE, 2),
                "Status": state,
            })
        df = pd.DataFrame(table)
        invested = df["Paid (€)"].sum()
        profit = (df["Receives (€)"] - df["Paid (€)"]).sum()
        weighted = (df["Yield a year (%)"] * df["Paid (€)"]).sum() / invested
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Invested", eur(invested))
        m2.metric("Expected profit", eur(profit))
        m3.metric("Average yield a year", pct(weighted))
        m4.metric("Spread vs risk-free", f"{weighted - RISK_FREE:+.2f} pts",
                  f"risk-free = {pct(RISK_FREE)}", delta_color="off")
        st.dataframe(df, hide_index=True)
        shares = df.groupby("Buyer")["Paid (€)"].sum() / invested * 100
        st.markdown("**Share of the money invested, by buyer (%)**")
        st.bar_chart(shares.rename("Share (%)"))
        top = shares.max()
        if top > 50:
            st.warning(f"Concentration: {top:.0f} % of the money depends on {shares.idxmax()}. "
                       "Buy invoices from other buyers to spread the risk.")
        else:
            st.success(f"Diversified: no buyer weighs more than {top:.0f} % of the portfolio.")

# ---- Tab 6: ledger explorer ----------------------------------------------------
with tab_chain:
    ok_chain, bad_index, why = verify_chain(s["chain"])
    if ok_chain:
        st.success(f"Chain valid: {len(s['chain'])} blocks, every block matches its hash "
                   "and points to the one before.")
    else:
        st.error(f"Chain broken at block #{bad_index}: {why}. Rewriting history is detectable.")
    st.dataframe(pd.DataFrame([{
        "#": b["index"],
        "Time (simulated)": b["time"],
        "Event": b["event"],
        "Signed by": s["names"].get(b["signer"], short(b["signer"])),
        "Status": "✅ success" if b["status"] == "success" else "⛔ reverted",
        "Hash": short(b["hash"]),
        "Previous hash": short(b["prev_hash"]),
    } for b in s["chain"]]), hide_index=True)
    pick = st.selectbox("Open a block", [b["index"] for b in s["chain"]],
                        index=len(s["chain"]) - 1, key=f"block_pick_{ver}_{len(s['chain'])}",
                        format_func=lambda i: f"#{i} · {s['chain'][i]['event']}")
    with st.expander(f"Block #{pick}: full content", expanded=False):
        st.json(s["chain"][pick])
    st.divider()
    st.markdown("**Tamper test**: someone rewrites the tokenised invoice to say €80,000 instead of €50,000.")
    has_invoice = any(b["event"] == "INVOICE_TOKENISED" for b in s["chain"])
    t1, t2 = st.columns(2)
    if t1.button("🕵️ Rewrite the invoice block", disabled=not has_invoice or bool(s["tamper"])):
        run(tamper_invoice, s, ok="Invoice block rewritten. Look at the integrity check at the top of this tab.")
    if t2.button("↩️ Restore the original block", disabled=not s["tamper"]):
        run(restore_invoice, s, ok="Original block restored: the chain is valid again.")

# ---- Tab 7: rules --------------------------------------------------------------
with tab_rules:
    st.subheader("Rules the contract enforces")
    st.table(pd.DataFrame([
        {"Rule": "One invoice = one token: same fingerprint or same supplier + number is refused",
         "Source": "Factoken design", "Why": "Stops double financing, the risk probed at First Brands (2025)"},
        {"Rule": "Payment term ≤ 60 days after the invoice date",
         "Source": "Code de commerce, art. L441-10", "Why": "Tokenisation reverts above the cap"},
        {"Rule": "Only invoices approved by the buyer can be funded",
         "Source": "Factoken design", "Why": "The investor buys a debt the buyer has confirmed"},
        {"Rule": "The buyer pays the contract, which pays the token holder",
         "Source": "Assignment notified to the debtor (Code civil, art. 1324)",
         "Why": "The investor gets paid, not the supplier's old bank account"},
        {"Rule": f"Late payment: {PENALTY_RATE} % a year + €{RECOVERY_FEE:.0f} per invoice",
         "Source": "Code de commerce, art. L441-10 (ECB rate + 10 pts)", "Why": "Added automatically"},
        {"Rule": f"Price = amount × (1 − rate × days / 360), rate = {pct(RISK_FREE)} + spread by rating",
         "Source": "Factoken pricing (assumption)", "Why": "Transparent, same rule for everyone"},
    ]).set_index("Rule"))
    m1, m2 = st.columns(2)
    with m1:
        st.markdown("#### This MVP")
        st.markdown("- Streamlit interface (Python)\n"
                    "- Contract as a Python state machine\n"
                    "- SHA-256 hash-chained ledger, one block per transaction\n"
                    "- Simulated EURC wallets and fictional companies")
    with m2:
        st.markdown("#### Production target")
        st.markdown("- Invoices received from certified e-invoicing platforms (mandatory since Sept 2026)\n"
                    "- Audited Solidity registry on an EVM layer-2, one token per invoice\n"
                    "- EURC, a MiCA-compliant euro stablecoin\n"
                    "- Receivables bought through a licensed financing partner or a securitisation fund\n"
                    "- Only fingerprints on-chain: invoice data stays off-chain (GDPR, business secrecy)")

st.caption("Factoken MVP · NEOMA MSc Fintech & DeFi · simulated chain and wallets, fictional companies; no real money.")
