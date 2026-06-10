# nostrmix — User's Guide

How to use the mixer as a **participant** (not the operator). You drive the whole
thing from private Nostr DMs, and **you keep custody the entire time**: the bot
only ever builds an *unsigned* transaction and asks you to sign it. Nothing moves
until you've inspected it and added your signature in your own wallet.

> Mixing involves real bitcoin. Read [§9 — Check the PSBT isn't cheating you](#9-check-the-psbt-isnt-cheating-you)
> before you ever sign, and start with small amounts until you trust the flow.

**Contents**

1. [What you need](#1-what-you-need)
2. [What mixing is](#2-what-mixing-is)
3. [Best practices](#3-best-practices)
4. [Command reference](#4-command-reference)
5. [Conforming vs non-conforming](#5-conforming-vs-non-conforming)
6. [Find open mixes](#6-find-open-mixes)
7. [Join — or start — a mix](#7-join--or-start--a-mix)
8. [Declare your inputs and outputs](#8-declare-your-inputs-and-outputs)
9. [Receive the PSBT](#9-receive-the-psbt) · [Check it isn't cheating you](#9-check-the-psbt-isnt-cheating-you)
10. [Sign the PSBT](#10-sign-the-psbt)
11. [Return the signed PSBT](#11-return-the-signed-psbt)
12. [Toxic change](#12-toxic-change)
13. [Gotta keep 'em separated](#13-gotta-keep-em-separated)
14. [Re-mixing for more privacy](#14-re-mixing-for-more-privacy)

---

## 1. What you need

- **A Nostr client that supports NIP-17 private DMs** (gift-wrapped direct
  messages) — e.g. 0xchat or Amethyst. Confirm your client implements NIP-17;
  older NIP-04 DMs won't reach the bot privately.
- **The bot's npub** (published by the operator). All commands are DMs to it.
- **A wallet that imports and signs PSBTs** — **Sparrow** or **Electrum** are the
  easiest. You sign there; the bot never sees your keys.
- **Native SegWit (`p2wpkh`, `bc1q…`) coins and addresses.** This mixer currently
  accepts `p2wpkh` inputs and pays to `p2wpkh` outputs only.

---

## 2. What mixing is

(The 10-second version.) Several people pool inputs into **one** transaction that
pays out many **identical-sized** outputs. Because those equal outputs are
indistinguishable, an observer can't tell which output belongs to which input —
the 1-to-1 link between your coin and your identity is broken. The leftover that
*isn't* a round denomination ("change") is the weak spot; see
[§12](#12-toxic-change) and [§14](#14-re-mixing-for-more-privacy).

---

## 3. Best practices

- **Prefer conforming amounts.** A UTXO that is *exactly* the mix's output size
  passes straight through with **no change and no miner fee** — the cleanest,
  cheapest, most private case. See [§5](#5-conforming-vs-non-conforming).
- **Give enough addresses**, all **fresh** (never reused). One per mixed output
  **plus one for change**. Too few and you mix fewer outputs and get a big,
  traceable change (the bot will warn you).
- **Always verify the PSBT before signing** ([§9](#9-check-the-psbt-isnt-cheating-you)).
- **Don't co-spend** your change together with your mixed outputs afterward
  ([§13](#13-gotta-keep-em-separated)).
- **Sign promptly.** There's a signing deadline (default **48h**). Miss it and
  you're treated as a ghost — blacklisted, and the mix re-forms without you.
- **Re-mix** change in a later round for stronger privacy ([§14](#14-re-mixing-for-more-privacy)).

---

## 4. Command reference

Everything is a DM to the bot. Commands start with `/` (case-insensitive).

| Command | What it does |
|---|---|
| `/list` (or `open`, `mixes`) | Show open mixes you can join |
| `/join <mix_name>` | Register interest in a specific open mix |
| `/commit <txid:vout> ...` | Declare the UTXO(s) you'll contribute (may be sent more than once) |
| `/addresses <addr1> <addr2> ...` | Give your fresh output addresses |
| `/psbt_accept <hex>` | Return your **signed** PSBT |
| `/psbt_chunk <i>/<n> <hex>` | Return a signed PSBT in pieces (only if it's very large) |
| `/cancel [mix_name]` (or `exit`, `leave`) | Leave a mix (auto-detects if you're in exactly one); you're refunded any service fee |

If you send something the bot doesn't understand, it replies with this same list.

---

## 5. Conforming vs non-conforming

Every UTXO you commit is classified against the mix's **output size** (shown in
`/list`, e.g. `0.01000000 BTC`):

- **Conforming** — amount is **exactly** the output size. It moves **1 input → 1
  output** to a fresh address of yours. **No change, no miner fee, no service
  fee.** This is a perfect pass-through and the best privacy you can get here.
- **Non-conforming** — any other amount. It's carved into as many equal
  output-size outputs as it can fund, plus a **change** output for the leftover.
  The owner of a non-conforming input **pays the miner fee** for the carving (and
  a service fee only if the operator has enabled one — it's **off by default**).

You can bring a mix of both. Conforming UTXOs are always welcome and free — they
grow everyone's anonymity set.

---

## 6. Find open mixes

DM the bot:

```
/list
```

It replies with each open mix: its name, output size, how many mixers it's
waiting for, and how many same-size (conforming) UTXOs it'll take for free, e.g.:

```
mix silver-cupcake: 0.0100 BTC outputs (collecting). Needs 2 mixer(s). Up to 10
same-size (0.0100 BTC) UTXOs welcome free of charge. p2wpkh addresses only.
```

---

## 7. Join — or start — a mix

**Join an existing mix** by name:

```
/join silver-cupcake
```

The bot registers your interest and asks for your UTXOs and addresses. If you
type the two words with a space instead of the hyphen — `/join silver cupcake` —
the bot understands that too.

**Start a new mix:** there's no separate "create" command and you can't pick the
size — mixes use the operator's defaults (typically **0.01 BTC** outputs,
`p2wpkh`). If no compatible mix is open, simply **`/commit` a UTXO** (next step)
and the bot spins up a fresh default mix and puts you in it automatically.

> One at a time: finish `/commit` **and** `/addresses` (and pay, if a fee is
> charged) for your current mix before joining another. You can be in up to a few
> *paid* mixes at once (default cap 5).

---

## 8. Declare your inputs and outputs

**Commit your UTXO(s)** — `txid:vout`, separated by spaces **or** commas. You can
send `/commit` more than once to add more:

```
/commit 4a5f…e1:0 9c2b…7d:1
/commit 4a5f…e1:0, 9c2b…7d:1
```

The bot looks each one up on-chain (must be unspent, confirmed, `p2wpkh`, and
above the dust floor) and tells you which it accepted or rejected.

> **Where to find `txid:vout`:** in **Electrum**, open the *Coins* tab — the long
> "output point" column **is** the `txid:vout`. In **Sparrow**, open the *UTXOs*
> tab — it's the "Transaction Output" value. Copy that string verbatim.

**Send fresh output addresses** — `bc1q…`, separated by spaces **or** commas:

```
/addresses bc1qaaa… bc1qbbb… bc1qccc…
/addresses bc1qaaa…, bc1qbbb…, bc1qccc…
```

> **Send one address for every output you'll get, PLUS one extra for change.**
> This is the single most important thing to get right. The number of addresses
> you provide is the hard cap on how many outputs you receive, so too few means
> you mix fewer coins than you could.

How many is that, concretely:
- **One per conforming UTXO** (each passes through 1→1), **plus**
- For a non-conforming contribution: **one per equal output it can fund, plus one
  more for change.** Example: a 2,500,000-sat input in a 0.01 BTC (1,000,000-sat)
  mix funds **two** equal outputs and leaves ~500,000 change → send **three**
  addresses (two mixed + one change).

If you send too few, the bot won't burn your coins — it turns your last address
into an (oversized) change output and **warns you** that adding an address would
mix more and shrink that change. So heed that warning and re-send `/addresses`
with one more. (With only a *single* address and an above-dust leftover, that
excess is donated or folded into the miner fee — another reason to always include
a change address.)

**Pay the service fee, if any.** It's **off by default** (you'll see "No service
fee — you're all set"). If the operator enabled one, the bot tells you how many
sats to zap and to which address; you advance once it's paid.

---

## 9. Receive the PSBT

Once the mix has enough participants, the bot assembles the transaction and DMs
you, over the same NIP-17 thread:

1. **Your miner-fee share**, e.g. `Your share of the miner fee: 270 sats …`.
2. **The unsigned PSBT**, delivered as a message that begins `/psbt_accept
   70736274ff…`. (If it's very large it arrives in pieces as `/psbt_chunk
   1/3 …`, `2/3 …` — concatenate the hex in order to get the whole PSBT.)

Copy out that PSBT hex. **Do not sign yet** — verify it first.

<a name="9-check-the-psbt-isnt-cheating-you"></a>

### Check the PSBT isn't cheating you

The bot *builds* the transaction (each output's locking script is derived from
the address you gave) and you only *sign*. So **you** must confirm it pays you
correctly before you authorize it. The bot signs with `SIGHASH_ALL`, so the
moment you sign, the outputs you verified are **frozen** — nobody can change them
afterward. Check three things:

1. **Inputs** — exactly your committed UTXO(s) are being spent, and nothing of
   yours you didn't intend.
2. **Outputs** — each of *your* addresses appears, paid the amount you expect,
   and every output is a normal, spendable address (no `OP_RETURN`/nonstandard
   scripts that would **burn** coins).
3. **Fee** — the miner fee is reasonable for the size (the real risk is value
   quietly siphoned into an absurd fee).

**Option A — Sparrow / Electrum (no install).** Load the PSBT — in **Sparrow**:
*File → Open Transaction → From Text*; in **Electrum**: *Tools → Load Transaction
→ From Text*. The wallet decodes each output back into an **address** and shows
the **fee**. Confirm your receive addresses appear with the right amounts and the
fee is sane. "It loaded without error" is **not** enough — read the outputs and
the fee.

**Option B — `psbt_decode.py` (if you have this repo).** Fully local, no node:

```bash
python scripts/psbt_decode.py <psbt-hex> --mine bc1qYOURS,bc1qALSO_YOURS
```

It lists every output as an address + amount + type, marks yours `<= YOURS`,
totals them, computes the fee from the PSBT's input amounts, and flags any
unspendable output as `*** UNSPENDABLE ***` (→ **do not sign**). A PSBT is the
right thing to decode here — it carries input amounts, so the fee is shown; a
finalized raw transaction doesn't.

If anything is off — wrong amount, an address that isn't yours, a crazy fee, an
unspendable output — **don't sign.** Use `/cancel` and ask the operator.

---

## 10. Sign the PSBT

In **Sparrow**: with the transaction open (*File → Open Transaction → From Text*),
review the inputs/outputs/fee one more time, click **Sign**, then **Export/Save**
the *signed* PSBT as text/hex.

In **Electrum**: *Tools → Load Transaction → From Text* → **Sign** → **Export/Copy**
the signed PSBT.

You sign **only your own inputs** — which is all you *can* sign anyway, since you
only hold those keys.

---

## 11. Return the signed PSBT

DM the bot the signed hex:

```
/psbt_accept 70736274ff…<your signed PSBT>…
```

Only if it's very large — **over ~50,000 hex characters (~25 KB)**, the bot's
chunking threshold — send it in numbered pieces instead (split the hex into
in-order chunks):

```
/psbt_chunk 1/2 70736274ff…
/psbt_chunk 2/2 …rest…
```

In practice a 2-5 participant mix produces a PSBT far smaller than that, so a
single `/psbt_accept` almost always fits — only large mixes need chunking.

The bot cryptographically verifies your signature against the exact skeleton it
sent. When **everyone** has returned a valid signature, it combines them,
finalizes, and broadcasts — then DMs you the transaction id. Done: your mixed
outputs are on-chain.

> Heads-up on the deadline: you have **48h** (default) to return your signature.
> If you ghost, you're blacklisted and the mix re-forms without you. If *someone
> else* ghosts after seeing your addresses, the bot discards your addresses for
> privacy and asks for fresh ones — just send a new `/addresses …`.

---

## 12. Toxic change

The equal mixed outputs are private; your **change** is not. Change is a unique
amount, and the transaction's arithmetic (`your inputs − your mixed outputs − fee
= your change`) lets a chain analyst re-link that change output back to the
inputs you put in. So **treat change as "tainted"**: it still points at you even
though your mixed coins don't. The fixes are §13 and §14.

---

## 13. Gotta keep 'em separated

**Never spend your change together with your mixed outputs** in a later
transaction. The instant you co-sign a tainted change output alongside a freshly
mixed one, you hand the observer a link between them — and you've undone the mix
for those coins. In your wallet, keep mixed coins and change in separate
accounts/labels and spend them apart. (Coin-control in Sparrow/Electrum lets you
choose exactly which UTXOs a payment spends.)

---

## 14. Re-mixing for more privacy

One pass with a handful of participants is a modest anonymity set. Privacy
compounds when you:

- **Bring conforming amounts** so you produce **no change** at all — nothing to
  re-link (see [§5](#5-conforming-vs-non-conforming)). This is the single biggest
  lever.
- **Re-mix** your outputs (and especially your toxic change) in **later rounds**.
  Each additional round multiplies the set of coins yours could be, and re-mixing
  change is how you launder the one part that was still traceable.

A practical pattern: split a non-conforming amount, mix it, then send the change
back through a future mix — ideally as a conforming-sized input — until what's
left of the traceable change is negligible.

---

## Quick reference: the happy path

```
/list                                  → see open mixes
/join silver-cupcake                   → join the mix you picked
                                         (or skip /join and just /commit to auto-start a new mix)
/commit <txid:vout> …                  → register the coins you'll contribute
/addresses bc1q… bc1q… bc1q…           → one per mixed output + one extra for change
   (pay the zap only if a fee is charged — off by default)
…bot DMs your fee share + the PSBT…
   → VERIFY it (psbt_decode.py / Sparrow): inputs, outputs (your addrs), fee
   → SIGN it in your wallet
/psbt_accept <signed-hex>              → return it
…bot broadcasts and DMs you the txid…
```

Changed your mind at any point before signing? `/cancel`.
