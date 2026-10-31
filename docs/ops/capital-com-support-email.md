# Szkic maila do Capital.com support

Wyślij z adresu przypisanego do konta, przez formularz kontaktowy lub czat w aplikacji. Mail jest po angielsku. W miejscu oznaczonym `<...>` wpisz swoje imię i nazwisko. Nie podawaj w nim haseł, kluczy API ani numerów dokumentów. Odpowiedzi zachowaj na piśmie (zrzuty ekranu lub e-mail), bo mają być podstawą decyzji o bramce A.

**Subject:** Written confirmation: automated trading via your public API (individual retail client, Netherlands)

Hello Capital.com team,

I am a private individual and a resident of the Netherlands (temporarily staying in Poland). I plan to run a small, rule-based program that trades one index CFD (US100) through your public REST and WebSocket API, at most one trade per day, with a stop-loss attached to every position, no high-frequency activity. I will start on the demo environment only and will not fund the live account until I have your written answers. I could not find the answers below in your documentation, so I would be grateful for written replies.

**Entity and terms**
1. With which Capital.com legal entity would my account be as a Netherlands resident today, and which version of the client agreement applies? Will the FxPro client transfer change my contracting entity or terms?
2. Is trading through your public API by a rule-based program running unattended permitted for retail clients? Please confirm how you interpret clauses 8.12(b) and 13.1.1(h) ("robots, high speed trading system") for this kind of low-frequency use.

**API access**
3. Can an API key be generated for the demo environment before account verification is completed? Is the same API key valid for both demo and live, and can a key be restricted to demo or read-only?
4. Is there a documented way to confirm an order outcome after a timeout (for example by `dealReference`), and is there any idempotency mechanism for position creation?

**Instrument rules (US100, US500, DE40)**
5. For each: minimum size, size step, value per point, margin requirement, minimum stop distance, guaranteed stop availability for Netherlands retail clients, trading hours and daily breaks.
6. How is a stop-loss executed in a price gap? Is it filled at the next available price?

**Costs**
7. Is the 0.4% currency conversion markup applied to CFD profit and loss when the account currency differs from the instrument currency? Which account currencies are available?
8. At what time is overnight financing charged, and does that time move with daylight saving? Are there any other fees on index CFDs (for example the inactivity fee)?

**Demo versus live**
9. How closely do demo prices, spreads and fills match live? Please list known differences (slippage, requotes, stop execution).

**Account and statements**
10. Does the account use netting or hedging by default, and can this be set for API use? Which statements or exports are available for an annual tax return?
11. Is there any restriction on accessing the account or the API from another EU country (Poland) during a temporary stay?

Thank you for your help.

Kind regards,
<imię i nazwisko>
