# Szkic maila do Alpaca support

Wyślij z własnego adresu na `support@alpaca.markets`. Mail jest po angielsku. W miejscach oznaczonych `<...>` wpisz własne dane. Nie podawaj w nim haseł, kluczy API ani numerów dokumentów.

**Subject:** Eligibility and rules for an individual trader in Poland trading US stocks via API

Hello Alpaca team,

I am a private individual and a tax resident of Poland. I plan to trade US stocks through your Trading API, first on a paper account and later, if my testing justifies it, with a very small live account (initially around 20 USD). I could not find answers to the points below in your documentation, so I would be grateful for written replies.

**Eligibility and funding**
1. Can a tax resident of Poland open an individual live brokerage account with Alpaca Securities LLC to trade US stocks through the Trading API? Which identification and proof-of-address documents are accepted, and is any "reverse solicitation" statement required?
2. Is there any minimum deposit or minimum equity for non-US residents today? What are the supported ways and fees to deposit and withdraw small amounts from Poland (for example the local currency transfer, its 1.5% fee, and any inbound wire fee)?

**Automated trading and fees**
3. Is fully automated trading through the API, without my approving each individual order, permitted for a retail account?
4. Your fee schedule mentions that order flow "determined to be non-retail in nature" may be charged. What defines non-retail flow and what thresholds apply?
5. Regulatory pass-through fees (SEC, TAF, CAT): are they aggregated per day per account and then rounded up to the next cent, or rounded per fill? Is the FINRA TAF currently waived and until when?

**Account rules for a small account**
6. For an account below 2,000 USD equity, how are good-faith violations and free-riding handled after the June 2026 intraday margin change? How do `buying_power`, `cash` and `non_marginable_buying_power` behave with unsettled proceeds on a 1x account?
7. What is the documented Trading API rate limit, the maximum number of `trade_updates` connections per account, and how are resting orders handled when a halt begins?

**Taxes**
8. Which tax documents do you provide to non-US clients (annual statements, CSV exports of trades and fees, Form 1042-S, W-8BEN status)?

Thank you for your help.

Kind regards,
<imię i nazwisko>
