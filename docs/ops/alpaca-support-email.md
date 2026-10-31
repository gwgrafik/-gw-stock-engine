# Szkic maila do Alpaca support (wersja 2, po researchu z 2026-10-31)

Wyślij z własnego adresu na `support@alpaca.markets`. Mail jest po angielsku. W miejscu oznaczonym `<...>` wpisz swoje imię i nazwisko. Nie podawaj w nim haseł, kluczy API ani numerów dokumentów.

Zmiany względem wersji 1: dodane pytania o PESEL/NIP jako Tax ID i o oświadczenie o własnej inicjatywie, o opłatę za przelew wychodzący (35 czy 50 USD) oraz o sprzeczność w naliczaniu opłat regulacyjnych. Pytanie o handel automatyczny zostało skrócone do prośby o potwierdzenie.

**Subject:** Eligibility and rules for an individual trader in Poland trading US stocks via API

Hello Alpaca team,

I am a private individual and a tax resident of Poland. I plan to trade US stocks through your Trading API, first on a paper account and later, if my testing justifies it, with a very small live account (initially around 20 USD). I could not find answers to the points below in your documentation, so I would be grateful for written replies.

**Eligibility**
1. Is tax residence in Poland supported for individual live accounts that trade US stocks through the Trading API? Is a Polish PESEL or NIP accepted as the Tax ID? Is any own-initiative ("reverse solicitation") declaration required?

**Funding**
2. Is there any minimum deposit or minimum equity for non-US residents today? Is the local currency transfer (1.5%, capped at 40 USD) available to individuals in Poland? Is the outbound international wire fee 35 USD (fee schedule revised October 12, 2026) or 50 USD (funding guide)? Can a balance of around 20 USD be withdrawn in practice?

**Automated trading and fees**
3. Please confirm that fully automated trading through the API, without my approving each order, does not by itself trigger a non-retail account designation, and tell me whether any thresholds apply.
4. Your fee schedule says regulatory fees are aggregated daily per account and rounded up to the next cent for each fee type, while an older FAQ says TAF and CAT are charged per trade. Which applies today? The TAF is listed at 0 USD per share: is it waived, and until when?

**Account rules for a small account**
5. For an account below 2,000 USD equity, how are good-faith violations and free-riding handled after the June 2026 intraday margin change? How do `buying_power`, `cash` and `non_marginable_buying_power` behave with unsettled proceeds on a 1x account?
6. What is the documented Trading API rate limit, the maximum number of `trade_updates` connections per account, and how are resting orders handled when a trading halt begins?

**Taxes**
7. Which tax documents do you provide to non-US clients (annual statements, CSV exports of trades and fees, Form 1042-S, W-8BEN status)?

Thank you for your help.

Kind regards,
<imię i nazwisko>
