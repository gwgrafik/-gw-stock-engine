# Szkic maila do Capital.com support (wersja 2, po researchu dla rezydenta NL)

Wyślij z adresu przypisanego do konta, przez formularz kontaktowy lub czat w aplikacji. Mail jest po angielsku. W miejscu oznaczonym `<...>` wpisz swoje imię i nazwisko. Nie podawaj w nim haseł, kluczy API ani numerów dokumentów. Odpowiedzi zachowaj na piśmie (e-mail lub zrzut czatu), bo mają być podstawą decyzji o bramce A.

Zmiany względem wersji 1: dla klientów z Holandii obowiązują warunki Capital Com Group Ltd (CySEC 463/25), T&C v2.1 z września 2026, w których roboty i algorytmy są dozwolone „tylko gdy wyraźnie dozwolone" (pkt 8.12.3(b)), a pkt 8.24.8 mówi o systemach dostępnych w platformie, np. MetaTrader. W wyciągniętym tekście nie ma słowa „API". Dlatego pytanie o zgodę na API jest najważniejsze i dotyczy też demo. Przed wysłaniem sprawdź w koncie (dokumenty prawne), którą wersję warunków zaakceptowałeś, i wpisz ją w pytaniu 1.

**Subject:** Written confirmation: use of your public API by a rule-based program (individual retail client, Netherlands)

Hello Capital.com team,

I am a private individual and a resident of the Netherlands (temporarily staying in Poland). I plan to run a small rule-based program that trades one index CFD (US100) through your public REST and WebSocket API, at most one trade per day, with a stop-loss attached to every position, and no high-frequency activity. I will start on the demo environment only and will not fund the live account until I have your written answers.

**Entity and terms**
1. With which Capital.com legal entity is my account (I accepted version `<wersja z konta>` of the terms)? I understand the current EU terms are v2.1 of Capital Com Group Ltd. Will the FxPro client transfer change my contracting entity or terms?
2. Clause 8.12.3(b) allows robots and algorithms "only when clearly permitted", and clause 8.24.8 refers to systems featured in the trading platform such as MetaTrader. The terms I read do not mention the public API. Is my use of the public REST/WebSocket API by a rule-based program running unattended permitted for retail clients, under which clause, and does the answer differ for the demo and the live account? Please confirm in writing.

**API access**
3. Can an API key be generated for the demo environment before account verification is completed? Is the same key valid for demo and live, and can a key be restricted to demo or read-only?
4. Is there a documented way to confirm an order outcome after a timeout (for example by `dealReference`), and is there any idempotency mechanism for position creation?

**Instrument rules (US100, US500, DE40)**
5. For each: minimum size, size step, value per point, margin requirement, minimum stop distance, guaranteed stop availability for Netherlands retail clients, trading hours and daily breaks.
6. How is a stop-loss executed in a price gap? Is it filled at the next available price?

**Costs**
7. Is the 0.4% currency conversion markup applied to CFD profit and loss when the account currency differs from the instrument currency? Which account currencies are available?
8. At what time is overnight financing charged, and does that time move with daylight saving? Are there other fees on index CFDs (for example the inactivity fee)?

**Demo versus live**
9. How closely do demo prices, spreads and fills match live? Please list known differences (slippage, requotes, stop execution). Does a demo account expire?

**Access and statements**
10. Clause 3.6.2 says access from a country other than the country of domicile "may not be permitted". Is there any restriction on accessing the account or the API from Poland during a temporary stay while my declared domicile and tax residence remain the Netherlands?
11. Does the account use netting or hedging by default, and can this be set for API use? Which statements or exports are available for an annual tax return?

Thank you for your help.

Kind regards,
<imię i nazwisko>
