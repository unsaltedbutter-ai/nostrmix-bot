# nostrmix — User's Guide

How to use the mixer as a **participant** (not the operator). You drive the whole
thing from private Nostr DMs, and **you keep custody the entire time**: the bot
only ever builds an *unsigned* transaction and asks you to sign it. Nothing moves
until you've inspected it and added your signature in your own wallet.

> ⚠️ Mixing involves real bitcoin. Read [§9 — Check the PSBT isn't cheating you](#9-check-the-psbt-isnt-cheating-you)
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
- **The bot's npub** — everything in this guide is a DM to:

  ```
  npub1g27uaw60wmt3j39a72rp73v36a49e0vmj4kw54l6pvyh3248dp7qzcj6jn
  ```
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
- **Don't treat the DMs as storage.** Every message the bot sends is marked to
  expire after about a week, so relays drop it and it can disappear from your
  client. Keep anything you want (a PSBT, your addresses) outside the chat.
- **Re-mix** change in a later round for stronger privacy ([§14](#14-re-mixing-for-more-privacy)).

---

## 4. Command reference

Everything is a DM to the bot — and **mostly, you just paste**:

- Send **`txid:vout` outpoints** and they're added as the *inputs* of your
  current mix.
- Send **bitcoin addresses** and they're added as your payout *outputs*. They
  accumulate across messages — one per message is fine; the bot replies with a
  running tally (`2 of 3 address(es) on file`).
- Send **signed PSBT hex** and it's taken as the signature for your inputs.

The bot recognizes each by its shape, so no command words are needed. Separate
items with spaces, commas, or new lines — and you can even put inputs and
addresses in the **same message**.

| You send | What happens |
|---|---|
| `list` | See the open mixes |
| `join 0.01` | Join (or start) a mix that pays out in 0.01 BTC pieces |
| `join silver-cupcake` | Join an open mix by its name |
| `<txid:vout> …` | Your coins go in as inputs (paste again to add more) |
| `bc1q… …` | Your payout addresses (they add up across messages) |
| `addresses clear` | Start your address list over |
| `70736274ff…` | Hand back your signed PSBT |
| `psbt_chunk <i>/<n> <hex>` | Signed PSBT in pieces, if it's too big for one DM |
| `cancel` | Leave the mix (any service fee is refunded) |
| `help` | The commands that matter at your current step |

Verb spellings are forgiving: `inputs` (= `input` = `commit`) before outpoints,
`addresses` (= `address` = `outputs`) before addresses, and `psbt_accept`
before a PSBT all work — as do `/list`, `open`, `mixes`, `exit`, `leave`,
`cancel <mix_name>`, and any capitalization.

If you send something the bot doesn't understand, it replies with a command list
tuned to your current step.

---

## 5. Conforming vs non-conforming

Every UTXO you send is classified against the mix's **output size** (shown in
`list`, e.g. `0.01000000 BTC`):

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
list
```

It replies with each open mix: its name, output size, how many mixers it's
waiting for, and how many same-size (conforming) UTXOs it'll take for free, e.g.:

```
silver-cupcake: 0.0100 BTC · needs 2 · +10 same-size free · p2wpkh
```

---

## 7. Join — or start — a mix

**The easy way: pick a size.** Tell the bot how big you want each mixed output
to be, in BTC:

```
join 0.01
```

If an open mix with **that exact output size** has room, you're in (the bot
picks the one closest to filling). If none exists, the bot **creates** one of
that size and puts you in it. Custom sizes work too (`join 0.00125` →
125,000-sat outputs); the only floor is the operator's minimum UTXO size. The
bot echoes back the size it created or joined, so a mistyped amount is visible
before you commit any coins. And if too many mixes are already open to start
another, it replies with the closest open sizes — e.g. `join 0.01 or join 0.02`
— so you can take the best fit.

**Or join by name**, picking one from `list`:

```
join silver-cupcake
```

💡 **The lazy way:** skip `join` entirely and just **paste a UTXO** (next
step) — the bot spins up a fresh default-size mix and puts you in it
automatically.

> ⚠️ One at a time: finish your inputs **and** outputs (and pay, if a fee is
> charged) for your current mix before joining another.

---

## 8. Declare your inputs and outputs

**Paste your UTXO(s)** — `txid:vout`, separated by spaces, commas, or new
lines. Paste again any time to add more:

```
4a5f…e1:0 9c2b…7d:1
4a5f…e1:0, 9c2b…7d:1
```

The bot looks each one up on-chain (must be unspent, confirmed, `p2wpkh`, and
above the dust floor) and tells you which it accepted or rejected.

> 💡 **Where to find `txid:vout`:** in **Electrum**, open the *Coins* tab — the long
> "output point" column **is** the `txid:vout`. In **Sparrow**, open the *UTXOs*
> tab — it's the "Transaction Output" value. Copy that string verbatim.

**Paste fresh payout addresses** — `bc1q…`. They **accumulate**: one address
per message is fine, and the bot replies with a running tally (`2 of 3
address(es) on file`) until you've sent enough. Pasted a wrong one?
`addresses clear` wipes the list to start over.

```
bc1qaaa… bc1qbbb… bc1qccc…
bc1qaaa…, bc1qbbb…, bc1qccc…
```

💡 In a hurry? Inputs and addresses can ride in **one message** — paste the
`txid:vout` lines and the `bc1q…` lines together and the bot files each where
it belongs.

> ⚠️ **Send one address for every output you'll get, PLUS one extra for change.**
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
mix more and shrink that change. So heed that warning and just paste one more
address. (With only a *single* address and an above-dust leftover, that excess
is donated or folded into the miner fee — another reason to always include a
change address.)

**Pay the service fee, if any.** It's **off by default** (you'll see "No service
fee — you're all set"). If the operator enabled one, the bot tells you how many
sats to zap and to which address; you advance once it's paid.

---

## 9. Receive the PSBT

Once the mix has enough participants, the bot assembles the transaction and
sends you two DMs:

1. **Your miner-fee share and the rate it was computed at**, e.g.
   `Your share of the miner fee: 270 sats (~0.00000270 BTC) at 1.5 sat/vB.`
   (Conforming-only participants see `0 sats` — pass-throughs ride free.)
2. **The unsigned PSBT** — a message containing **only** the PSBT hex
   (`70736274ff…`), nothing else, so select-all → copy grabs exactly the
   PSBT. (If it's very large it arrives in pieces as `psbt_chunk 1/3 …`,
   `2/3 …` — concatenate the hex parts in order to get the whole PSBT.)

**👀 Do not sign yet — verify it first.**

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
unspendable output — **don't sign.** Use `cancel` and ask the operator.

---

## 10. Sign the PSBT

In **Sparrow**: with the transaction open (*File → Open Transaction → From Text*),
review the inputs/outputs/fee before you sign — this is very important — then
click **Sign** and **Export/Save** the *signed* PSBT as a file.

In **Electrum**: *Tools → Load Transaction → From Text* → review → **Sign** →
**Export/Save** the signed PSBT as a file.

Wallets save the signed PSBT as a **binary `.psbt` file**; the bot needs it as a
**hex string**. Convert it in a terminal:

```bash
xxd -p signed.psbt | tr -d '\n'; echo
```

Copy the output — that `70736274ff…` string is what you DM back in the next step.

You sign **only your own inputs** — which is all you *can* sign anyway, since you
only hold those keys.

---

## 11. Return the signed PSBT

DM the bot the signed hex — just paste it (`psbt_accept` in front also works):

```
70736274ff…<your signed PSBT>…
```

Only if it's very large — **over ~50,000 hex characters (~25 KB)**, the bot's
chunking threshold — send it in numbered pieces instead (split the hex into
in-order chunks; this is the one command that needs its verb):

```
psbt_chunk 1/2 70736274ff…
psbt_chunk 2/2 …rest…
```

In practice a 2-5 participant mix produces a PSBT far smaller than that, so a
single pasted message almost always fits — only large mixes need chunking.

The bot cryptographically verifies your signature against the exact skeleton it
sent. If what you sent isn't actually signed — or signs the wrong inputs —
nothing is recorded: the bot replies with what's missing (e.g. `Input #0
(yours) was not signed`) and keeps waiting, so just sign, convert, and paste
again. When **everyone** has returned a valid signature, it combines them,
finalizes, and broadcasts — then DMs you the transaction id. Done: your mixed
outputs are on-chain.

> ⚠️ Heads-up on the deadline: you have **48h** (default) to return your signature.
> If you ghost, you're blacklisted and the mix re-forms without you. If *someone
> else* ghosts after seeing your addresses, the bot discards your addresses for
> privacy and asks for fresh ones — just paste new addresses.

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
list                                   → see open mixes
join silver-cupcake                    → join the mix you picked
   (or join 0.01 to join-or-start a mix of that size,
    or skip join — pasting a UTXO auto-starts a new mix)
<txid:vout> …                          → paste the coins you'll contribute
bc1q… bc1q… bc1q…                      → paste one address per mixed output
                                         + one extra for change (tally builds
                                         across messages; addresses clear resets)
   (pay the zap only if a fee is charged — off by default)
…bot DMs your fee share + the PSBT…
   → VERIFY it (psbt_decode.py / Sparrow): inputs, outputs (your addrs), fee
   → SIGN it in your wallet
70736274ff…<signed psbt hex>           → paste it back
…bot broadcasts and DMs you the txid…
```

Changed your mind at any point before signing? `cancel`.
