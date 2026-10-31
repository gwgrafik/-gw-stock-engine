# Can a Poland-resident private individual open and use a live Alpaca brokerage account (US stocks, Trading API) in 2026?

Research date: 2026-10-31. Confidence tags: [H] = official/primary and unambiguous, [M] = primary but indirect, or well-attributed secondary, [L] = anonymous, undated, old (pre-2025) or unverified.
Scope note: spec section 16 questions 1, 5, 7 (and partly 2, 4) of the stock-engine spec; draft support email in `docs/ops/alpaca-support-email.md`. Nothing was bought, no account was created, no credentials were entered.

---

## 1. Polish-language first-hand experiences (Bankier, Comparic, Wykop, Reddit PL, Facebook, blogs, YouTube)

### Takeaway
I found NO first-hand Polish report (approved, rejected or pending) of a Polish resident opening a live Alpaca Securities account, and no Polish page that names Poland as supported. The absence is real for what the search tools can reach, but Reddit, Facebook, Wykop and Bankier threads are poorly indexed or blocked, so "no evidence" is not "evidence of no".

### Cited Findings
- Polish-language web searches ("Alpaca konto Polska", "Alpaca broker rezydent Polski", "Alpaca Markets PIT-38 W-8BEN", "bankier forum Alpaca", "Alpaca Markets recenzja broker dla Polaków", and several Exa semantic queries describing a Polish user opening or being refused an account) returned only Alpaca's own docs, generic Polish tax guides (freenance.io, klopit.co.pl, BrokerChooser PL) and unrelated bot-framework pages. None contained a Polish user's account-opening story. [M] — e.g. [Freenance W-8BEN/PIT-38 guide 2026](https://freenance.io/inwestowanie/akcje-amerykanskie-z-polski-2026-w-8ben-podatki-dywidendy-pit-38/) (generic, no Alpaca mention), [BrokerChooser PL algo ranking, 2025-09-11](https://brokerchooser.com/pl/best-brokers/best-brokers-for-algo-trading)
- BrokerChooser's Polish-language ranking (published 2025-09-11) puts "Alpaca Trading" first for algo trading, says its analysts "opened accounts, generated API keys and placed test and real orders", and lists deposit methods "Przelew bankowy, Airwallex, Checkout, Currency Cloud, I2C, Nium, Sila Money, Wise" and withdrawal fee "$0". It does NOT state that Polish residents can open an account; the URL has no country filter, so it appears to be the global list (my reading). The English review page (updated 2026-04-07) header says "Available in the United States" yet also lists "Open to clients worldwide" as a pro, so it is ambiguous on country scope. [L] — [BrokerChooser PL](https://brokerchooser.com/pl/best-brokers/best-brokers-for-algo-trading), [BrokerChooser EN review, updated Apr 7 2026](https://brokerchooser.com/broker-reviews/alpaca-trading-review)
- A Czech university thesis (Brno, 2024) describes using an Alpaca paper account for an automated share-trading system and says a real-money account "would" require personal data; it reports no live-account attempt. Not live evidence. [L] — [Škorpík 2024](http://hdl.handle.net/11012/246609)
- Alpaca's own forum has no post mentioning Poland: the Discourse search endpoint for "Poland order:latest" returned zero posts and "Polish" returned only unrelated "polish/polished" hits (checked 2026-10-31). Caveat: anonymous Discourse search can miss content. [M] — [forum search "Poland"](https://forum.alpaca.markets/search.json?q=Poland%20order%3Alatest), [forum search "Polish"](https://forum.alpaca.markets/search.json?q=Polish)
- The EU-themed forum thread "Live trading for European citizens" (2019-12 to 2021-02) never mentions Poland; its only staff reply (Jason_Bohne_Alpaca, 2021-02-02) just points to the non-US invite-only beta blog post. [L, old] — [forum thread 584](https://forum.alpaca.markets/t/live-trading-for-european-citizens/584)

### Inferences
- Because Alpaca has very few Polish-language touchpoints (no Polish site, no Polish app), lack of Polish anecdotes is expected and says little about eligibility either way.
- The only way to resolve the Poland question from the user side is a direct test (Country of Tax Residence dropdown plus live application) or a written support reply.

### Gaps
- Reddit (r/Polska_Inwestuje, r/inwestowanie, r/Polska) could not be queried: the search tool blocks reddit.com and direct Reddit JSON fetches failed ("SOURCE_NOT_AVAILABLE"). Wykop, Comparic, Bankier forum and Facebook group content were not reachable with the available tools; Trustpilot returned HTTP 403. A human check of these (site search for "alpaca") is worthwhile.
- Not found: any report of Polish PESEL/NIP being accepted as "Tax ID", proof-of-address requirements for Poland, Polish bank-to-Alpaca funding timings, or any rejection reason for Polish applicants.

---

## 2. English-language first-hand reports (Alpaca forum, Reddit, review sites) from EU / non-US users

### Takeaway
Alpaca staff say in 2026 that non-US individuals (explicitly Singapore) are accepted and that accounts are held by Alpaca Securities LLC, and Alpaca's own guide gives Portugal as an accepted EU example. Country support is applied per tax residency; known refusals are Brazil, Indonesia, Canada (and in 2022 a Romanian signup error). No post from a Polish or any other named EU resident confirming an approved live account in 2025-2026 was found.

### Cited Findings
- Alpaca staff (Dan_Whitnable_Alpaca), 2026-09-30, answering a Singapore resident's questions: "Do you accept individual accounts from Singapore residents? Yes." / "Which legal entity would hold the account...? Accounts are held by Alpaca Securities LLC." / "Is there any Singapore-specific restriction...? No." / W-8BEN "must be resubmitted every three years in addition to the initial year signed." [H, for non-US individuals in general; Poland not named] — [forum "No reply from customer service", 2026-09-28/30](https://forum.alpaca.markets/t/no-reply-from-customer-service/19766)
- Alpaca staff, 2026-07-26: "Alpaca currently does not support accounts with a tax residency of Brazil. While your live account shows up in your dashboard it has a status of rejected due to this. That is why you cannot fund the account." The user had believed his account was approved and thought a name mismatch was blocking funding. This confirms the failure mode "application accepted into the dashboard, later shown as REJECTED for an unsupported tax residency, and funding is simply impossible". [H] — [forum thread 19370, post 10](https://forum.alpaca.markets/t/alpaca-forum-account-hijacking/19370/10)
- Alpaca staff on Indonesia: 2024-02-05 "Alpaca currently doesn't support live trading accounts where the account holder jurisdiction is Indonesia"; 2024-03-19 the country is determined by the "physical address one enters in the Alpaca account application" (matching ID and linked bank account address); a user reported on 2026-06-29 that support still says Indonesia is unavailable. [H] — [forum thread 11836](https://forum.alpaca.markets/t/country-of-tax-residence-indonesia/11836)
- Canada: forum thread "Can't create individual account in Canada" (18 replies, last updated 2026-06-10): Canada is not in the country list. Staff (per a secondary review) said on 2026-08-14 that Algo Trader Plus is not sold to Canadian residents for regulatory reasons. [M] — [forum Getting Started listing](https://forum.alpaca.markets/c/getting-started-with-alpaca/19.md?page=1), [amicobot.it, 2026-10-21](https://amicobot.it/blog/alpaca-trading/)
- EU examples: forum "Cannot create account in Romania" (2022-03-02): "trading is not supported in account region" at signup despite the website suggesting availability. [L, old] — [forum listing snippet](https://forum.alpaca.markets/latest.md?page=83). In 2020-05/06 users reported that many EU countries (Sweden, France, Germany, Netherlands) were selectable but Czech Republic and Belgium were not. [L, old] — [forum "Alpaca as a non-US citizen"](https://forum.alpaca.markets/t/alpaca-as-a-non-us-citizen/1523)
- Reddit thread "Looking for an EU broker with API + Fractional Shares (US Stocks)" (subreddit not shown in the snapshot; original post about 2026-01-05, i.e. "149 days ago" at a 2026-06-03 snapshot): a user based in Spain wrote he had ruled out Alpaca because "Their retail Trading API is US-only now. Their Europe Beta is Broker API (B2B/Institutional setup only)". One commenter replied only with Alpaca's "available outside the US" links; a German commenter recommended tastytrade instead. This is an unverified claim by an anonymous poster and contradicts Alpaca's official pages and staff reply of 2026-09-30, but it shows that at least one European developer concluded Alpaca individual access was closed to him. [L] — [Reddit snapshot](https://reddit.sentinel-team.org/posts/1tvoxp3/snapshots/2026-06-03T20%3A41%3A04.265276Z)
- Secondary review (Italy-focused, AI-assisted, affiliate links disclosed), 2026-10-21: lists user reports from European-looking non-residents: Trustpilot "Michal Z." 2024-10-08 (two years, 3,000+ real trades, zero commissions even outside the US; country not stated), Trustpilot "Michelangelo R." 2025-06-19 (using Alpaca since 2023, withdrawals usually next day, knows the broker is not regulated in his country), YouTube @ladiokd ~Nov 2025 (non-resident funded via Wise in minutes), Trustpilot "Pes Ces" 2026-08-07 (withdrawals rejected, fees charged on each attempt), Trustpilot "Thomas N." 2026-08-12 (transfer without required reference pending 12 days), forum bennycode 2024-10-16 (CurrencyCloud withdrawal stuck at 0 USD, fixed only by switching to wire). The site says it did not open an account itself. No Polish user is identified. [L–M] — [amicobot.it](https://amicobot.it/blog/alpaca-trading/)
- General support-quality signal, Sept 2026 (non-Polish): several forum threads about lost or unposted wire deposits and slow/AI-only support ("Wire Deposit Not in Account" 2026-09-14, "Lost Wire for account funding" 2026-09-14, a non-US user told by an AI reply that a large deposit needs extra documents). [L–M] — [forum latest page 2](https://forum.alpaca.markets/latest?no_definitions=true&page=2)

### Inferences
- Alpaca treats eligibility as a per-tax-residence allow-list that staff will not publish; both "dropdown missing" (Canada, Albania 2022) and "dropdown present but later REJECTED" (Brazil 2026) patterns exist, so seeing Poland in the dropdown is necessary but not proof.
- The Sept 30 2026 staff answer is the strongest 2026 primary evidence that individual non-US accounts at Alpaca Securities LLC are alive and unrestricted by a MiFID-type carve-out, but it concerns Singapore, not the EU.
- The Spanish Reddit claim probably reflects the user's own reading of the Alpaca Europe pages (which are B2B) rather than a refusal from Alpaca Securities LLC.

### Gaps
- No forum or Reddit post by a Polish, German, French, Spanish or other EU resident stating "my live account was approved in 2025/2026" was found; the only EU-resident success evidence is Alpaca's own Portugal example (section 3).
- Country of the Trustpilot/YouTube reviewers is not verified.
- No data on how long Polish-ID KYC takes or what a Polish "Tax ID" (PESEL vs NIP) is mapped to.

---

## 3. Alpaca's current official pages (country availability, non-US guide, FAQ)

### Takeaway
Alpaca's official wording is generic ("195+ countries", "many non US residents, depending on country of residence") and never names Poland; the dedicated "Countries Alpaca is available" page (Feb 2026) contains no list and says only "contact support". Portugal is the only EU country named as an eligible example; Canada is the named exclusion.

### Cited Findings
- "Countries Alpaca is available", dated February 2026, full text: "Please contact support at support@alpaca.markets for more information regarding whether your country is supported." No list is on the page. [H] — [alpaca.markets/support/countries-alpaca-is-available](https://alpaca.markets/support/countries-alpaca-is-available) (2023-2024 staff replies referred users to a country list; that list is not on the page as of the Feb 2026 version)
- "How to Open a Live Trading Account as a Non-US Resident?", header "This blog was last updated on March 11th, 2026":
  - "We support the services to 195+ countries. If you don't see your country in the list, please reach out to support for further details."
  - "Yes. Alpaca supports live brokerage accounts for many non US residents, depending on country of residence. Non US users can fund their accounts with as little as $1 after approval."
  - Examples: "A Portuguese tax resident may open a live account as a resident of Portugal." and "Canadian residents are not currently supported."
  - "Alpaca does not support every jurisdiction... To check if your country is supported, please contact our team at [support email]. Alpaca is a U.S.-based broker, and services are only available in jurisdictions where it is legally permitted to operate."
  - "Eligibility is determined primarily by your country of tax residence and physical residence. Your documentation must align with your declared country of tax residence."
  - Verification "generally requires: A valid Tax ID from your country of tax residence; A document containing your photo and residential address; A selfie". Individual application needs "A valid, government-issued ID (e.g., passport)... A document confirming your address, if requested (e.g., recent utility bill or bank statement)". "Your proof of residency and Tax ID must match your declared country of tax residence." W-8BEN is generated and agreed during onboarding. "There is no high minimum deposit requirement."
  - Poland and the EU/EEA are not mentioned anywhere on the page. No reverse-solicitation or non-solicitation statement appears in the page text. [H] — [alpaca.markets/learn/live-trading-account-non-us](https://alpaca.markets/learn/live-trading-account-non-us/)
- "How to Get Alpaca Trading API Key and Start Connection" (undated learn page): "If your country is not listed under Country of Tax Residence, you will only be eligible for paper trading." This makes the signup dropdown the practical eligibility test. [M] — [alpaca.markets/learn/connect-to-alpaca-api](https://alpaca.markets/learn/connect-to-alpaca-api)
- Docs: "We now support brokerage accounts for individuals and business entities from around the world"; Alpaca Securities LLC (FINRA member, SEC registered broker-dealer) onboards; "All accounts are opened as margin accounts. Accounts with $2,000 or more equity will have access to margin trading and short selling." Undated. [M] — [docs account-plans](https://docs.alpaca.markets/us/docs/account-plans)
- Support article "Is Alpaca available outside the US?" (page dated 2020-12-07): "we are lifting the $30,000 minimum deposit and allowing all non-US users to fund their accounts with as little as $1"; "KYC documents vary by country, but generally you require a Tax-ID, and document containing your photo and your address." [M, old] — [alpaca.markets/support/is-alpaca-available-outside-the-us](https://alpaca.markets/support/is-alpaca-available-outside-the-us)
- Older beta wording (2020-07-30): non-US live accounts were an invite-only beta "for 'non-solicited' customers only", requiring a Non-Solicitation Agreement certifying the customer "was not directly solicited to open a live brokerage account", initially with $30,000 funding. This is the origin of the "reverse solicitation" framing; current 2026 guides no longer mention it. [L, 6 years old] — [Alpaca blog 2020-07-30](https://alpaca.markets/blog/non-us-live-trading-beta/)
- Business-account marketing page: "we support sophisticated and institutional traders across 196 countries and regions... See all available countries." (business accounts, $30,000 minimum). [M] — [alpaca.markets/business-account](https://alpaca.markets/business-account)

### Inferences
- "195+ countries" plus "many non US residents, depending on country" plus Portugal as the only EU example means an EU resident is plausible but Alpaca deliberately avoids publishing the list, so Poland must be confirmed with support.
- Since the exclusions named in 2026 are Canada, Brazil and Indonesia (all with regulatory/payment frictions), and not any EU country, there is no evidence of an EU-wide block.

### Gaps
- The actual current content of the signup "Country of Tax Residence" dropdown was not inspected (it requires creating a paper account, which this task forbids).
- No official statement on Poland, Polish Tax ID types, or whether Alpaca still asks non-US applicants to sign a non-solicitation/reverse-solicitation agreement.

---

## 4. Alpaca Europe (WealthKernel / CNMV), EEA passporting, reverse solicitation and KNF

### Takeaway
Alpaca Europe and the July 2026 EEA passport (which names Poland) are explicitly B2B: they serve fintechs and financial institutions, not individuals signing up on alpaca.markets. A Polish individual's account would therefore be with the US entity Alpaca Securities LLC, which is not on the ESMA investment-firm register, so legality for Polish retail rests on the "client's exclusive initiative" exception that KNF narrows. No Alpaca staff statement on EEA individuals, reverse solicitation or MiFID was found.

### Cited Findings
- Alpaca blog / Business Wire, 2026-07-07: Alpaca "completed the process to passport our regulated and localized investment services across 29 countries in the EEA... through our Spain-based EEA hub"; passported countries explicitly include Poland; the purpose is "to support fintechs, financial institutions, and enterprise partners building regulated investment products across Europe"; "Businesses building investment products across the region can work with Alpaca for account infrastructure, custody, and trading capabilities without pursuing separate authorizations". No sentence offers individual retail accounts. [H] — [Alpaca blog](https://alpaca.markets/blog/alpaca-completes-eea-passporting-to-29-countries-expanding-access-to-regulated-investment-services-across-europe/), [Italian copy of the Business Wire release](https://www.ladige.it/business-wire/alpaca-completa-il-processo-per-ottenere-il-passaporto-in-29-paesi-ampliando-cosi-laccesso-a-servizi-di-t8o5fajd)
- Same release (Italian copy): "Alpaca Europe is the trading name of WealthKernel Limited" (FCA No. 723719) and "WealthKernel Spain, A.V., S.L.U. is authorised and registered with the CNMV (No. 328)"; "Securities brokerage services are provided by Alpaca Securities LLC (dba Alpaca Clearing), member of FINRA/SIPC". [H] — same Italian copy as above
- Alpaca Europe docs: "Our European platform is designed primarily for B2B partnerships. We work directly with Referral Partners—such as neobanks, crypto platforms, and wealth apps, enabling them to embed investing features directly into their applications for their end customers." Alpaca Europe "provides RTO and custody services for US equities under its CNMV-regulated subsidiary, Alpaca Europe A.V. SLU". [H] — [docs.alpaca.markets/eu about-alpaca](https://docs.alpaca.markets/eu/docs/about-alpaca)
- Alpaca Europe Broker API docs: for "Fintech Referral Partners... and European Investment Services Firms"; partners "act as the client-facing entity and must be appropriately authorised in your local jurisdiction"; all end-user deposits, balances and withdrawals are "exclusively in Euros (EUR)". There is no retail Trading API in the EU docs. [H] — [About Broker API (EU)](https://docs.alpaca.markets/eu/docs/about-broker-api), [Customer Account Opening (EU)](https://docs.alpaca.markets/eu/docs/account-opening), [Funding Accounts (EU)](https://docs.alpaca.markets/eu/docs/funding-accounts)
- alpaca.markets/eu is titled "Modern brokerage infrastructure API for Europe... Offer US and European equities on your platform". [H] — [alpaca.markets/eu](https://alpaca.markets/eu)
- CNMV register entry: Alpaca Europe, A.V., S.L.U., official register no. 328, registered 2025-04-25, service "Reception and transmission of orders". [H] — [CNMV](https://www.cnmv.es/portal/consultas/esi/esis?lang=en&nif=B75852608&vista=17)
- Secondary legal reading (Italy, 2026-10-21): on 2026-10-21 the only Alpaca entity in the ESMA investment-firm register is Alpaca Europe A.V., not Alpaca Securities LLC; "A private individual does not open an account with Alpaca Europe from the website"; a non-EU firm may serve retail clients only under the MiFID II Art. 42 exclusive-client-initiative exception. [M] — [amicobot.it](https://amicobot.it/blog/alpaca-trading/)
- KNF communication (updated 2021-03-01): non-EU firms may conduct brokerage in Poland only via a branch with a KNF licence; "an exception is where the client, of their sole initiative, requests a specific transaction, provided it does not occur in response to marketing by that firm"; KNF/ESMA warn against firms that advertise to EU investors and then get a "I agree" pop-up declaration of own initiative; such declarations do not make the service client-initiated if the firm solicits or promotes in the EU. [H] — [KNF komunikat](https://www.knf.gov.pl/komunikacja/komunikaty?articleId=72670&p_id=18)
- Alpaca staff statement on EEA individuals / reverse solicitation / MiFID: none found in 2025-2026 (forum, support pages, press). The only reverse-solicitation-style wording is the 2020 beta Non-Solicitation Agreement (section 3). [L — absence]

### Inferences
- The July 2026 passporting names Poland only because Alpaca Europe can now sign Polish fintech partners; it does not open a retail route for the owner, and the earlier research that this looks B2B is confirmed.
- Retail Polish access, if it exists, runs through the US entity under the client-initiative exception; Alpaca's active Poland-related marketing (passport press release naming Poland, global "195+ countries" claims) could, under the KNF/ESMA reading, weaken that footing. This is a risk on Alpaca's side (it could restrict EEA retail signups) rather than a legal violation by the user, but it is an unquantified continuity risk. This is a legal point for the Polish adviser in spec questions 14-16, not a finding.

### Gaps
- No KNF statement naming Alpaca (Alpaca does not appear in the search results I retrieved from the KNF public-warnings list; I did not do an exact-name query on the live list, so it is unconfirmed either way).
- No Alpaca statement on whether it applies a country block to EEA retail because of MiFID.
- Did not retrieve Alpaca's customer agreement (non-US) to check for an own-initiative clause.

---

## 5. Closures/restrictions, tax forms (W-8BEN, 1042-S) and small-account fees for a 20 USD account

### Takeaway
No account closures or tax-form problems specific to EU/Polish users were found. The Polish bank-to-Alpaca funding route that fits a 20 USD account is Local Currency Transfer through CurrencyCloud (Poland is on its supported list; 1.5% fee, so about 0.30 USD on 20 USD); a plain international wire (35 USD outbound per the Oct 2026 fee schedule) is unusable for withdrawing such a balance.

### Cited Findings
Tax forms and documents
- Non-US tax residents complete a W-8BEN during onboarding; it is generated from the application data. Staff (2026-09-30) confirmed it must be resubmitted every three years in addition to the year signed. EU docs: valid W-8BEN gives 15% treaty rate on US dividends, otherwise "the default 30% U.S. withholding rate will automatically apply". [H] — [learn non-US guide, updated 2026-03-11](https://alpaca.markets/learn/live-trading-account-non-us/), [forum 2026-09-30](https://forum.alpaca.markets/t/no-reply-from-customer-service/19766), [EU docs "European Accounts"](https://docs.alpaca.markets/eu/docs/international-accounts) (partner-facing)
- Form 1042-S: support page (April 2024) explains 1042-S issuance for foreign accounts; "the IRS only requires us to generate a 1042-S if the income received for the tax year is equal to or greater than $.50"; one 1042-S per income code; withholding on dividends posted as DIVNRA (the example uses India's 25% treaty rate). The Italian secondary review claims no 1042-S arrives if nothing was sold and dividends are under 10 USD; this conflicts with the official 0.50 USD threshold, so rely on the official page. [H] — [Alpaca support, April 2024](https://alpaca.markets/support/what-is-an-example-of-treaty-withholding-withholding-exemption-and-the-1042-s), conflicting [M] [amicobot.it](https://amicobot.it/blog/alpaca-trading/)
- No source found saying Alpaca issues a Polish PIT-8C or PLN-denominated statement. General Polish guidance: foreign brokers usually do not issue PIT-8C; gains reported by the investor in PIT-38 at 19%, converted with NBP rates; with W-8BEN dividends are 15% in the US plus about 4 percentage points in Poland. [M, generic] — [BrokerChooser "How to Buy US Stocks from Poland: 2026 Guide", 2026-07-02](https://brokerchooser.com/education/stocks/how-to-buy-us-stocks-from-poland)

Closures / restrictions
- Account statuses in the docs include `REJECTED` and `ACCOUNT_CLOSED`; `ACCOUNT_UPDATED` blocks outgoing transfers while personal data is re-reviewed, and edits to `country_of_tax_residence`, tax ID, name or address trigger it (partner docs). [M] — [EU accounts-statuses](https://docs.alpaca.markets/eu/docs/accounts-statuses)
- Forum users have hit "Error submitting additional info: permission denied to update country_of_tax_residence" (2026-07-10, country not stated) and "field country_of_tax_residence cannot be updated" (2022-02), i.e. the declared tax residence cannot simply be changed after creation. [L–M] — [forum 2026-07-10](https://forum.alpaca.markets/t/error-submitting-additional-info-permission-denied-to-update-country-of-tax-residence-or-not-recognized/19249), [forum listing 2022](https://forum.alpaca.markets/latest.md?page=83)
- Name-matching rule for funding: "bank accounts must be in your name to be linked and used for funding"; deposits can fail for "Bank account name mismatch" or "Bank side restrictions". [H] — [learn "How to Fund Your Alpaca Live Trading Account", updated 2026-01-26](https://alpaca.markets/learn/fund-live-trading-account/)
- No reports of closures/restrictions of Polish or other EU individual accounts were found (only the Brazil rejection in section 2).

Funding route and fees (relevant to a ~20 USD account)
- Wire: "If you are sending funds from an international bank, the wire must be sent in USD. Alpaca does not accept wires in other currencies. If your bank cannot send USD wires, we recommend using CurrencyCloud instead." Local currency transfer: "International users can send funds in their local currency to a CurrencyCloud wallet"; needs the IBAN; "there is a 1.5% fee (capped at 40 USD) on both incoming and outgoing transfers"; for wire/LCT withdrawals "you must leave enough cash to cover the fee, as it is charged as a separate line item"; non-ACH deposits must fully post before trading. [H] — [learn funding guide, updated 2026-01-26](https://alpaca.markets/learn/fund-live-trading-account/)
- CurrencyCloud support page lists Poland among regions supporting local currency transfer (also Germany, Czech Republic, etc.), says Revolut is not supported through CurrencyCloud, and "Alpaca charges a 1.5% transaction (maximum $40) fee." Undated (sibling international-funding articles are tagged November 2022). [M] — [Alpaca support: CurrencyCloud](https://alpaca.markets/support/currencycloud-transfer-questions)
- Brokerage Fee Schedule, "Revised on October 12, 2026": Domestic wire outbound 15 USD, International wire outbound 35 USD, Local Currency Transfers (inbound and outbound) "1.5% conversion fee *Max of $40 USD per transaction", ACH return 25 USD; commission-free for self-directed API accounts except index options, Elite Smart Router and "order flow determined to be non-retail in nature"; regulatory fees: "Each fee type is aggregated separately at the daily, per-account level. After aggregation, each fee total is rounded up to the nearest cent ($0.01)." This conflicts with the learn guide (Jan 2026: 25 USD domestic / 50 USD international wires) and a secondary summary of an April 2026 schedule (25/50), so the schedule dated Oct 2026 is the newest. [H] — [BrokFeeSched.pdf](https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf), conflicting [H, older] [learn funding guide](https://alpaca.markets/learn/fund-live-trading-account/), [LegalClarity summarising Apr 1 2026 schedule](https://legalclarity.org/what-is-the-alpaca-securities-charge-on-your-bank-statement/)
- Inbound wires: the Italian review says Alpaca charges nothing on incoming wires but the sending bank/FX costs apply; the official schedule lists no inbound wire fee. [M] — [amicobot.it](https://amicobot.it/blog/alpaca-trading/), [fee schedule](https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf)
- Withdrawal reliability reports: CurrencyCloud withdrawal stuck at 0 USD (forum, 2024-10-16, via secondary), CurrencyCloud funding-account-not-found 404 (forum, 2022-11-21; staff suggested it related to pending account status). [L] — [forum thread 11127](https://forum.alpaca.markets/t/currencycloud-seems-broken-cant-deposit-funds/11127), [amicobot.it](https://amicobot.it/blog/alpaca-trading/)

Spillover for other spec questions (primary, Alpaca staff, 2026-09-30, [forum thread](https://forum.alpaca.markets/t/no-reply-from-customer-service/19766)) [H]
- Non-retail flow (spec Q2): "Automated API trading does not in itself trigger a non-retail account designation." Options-order professional rule: averaging 390+ options orders per day over a month.
- No inactivity fee; "Minimum to open, and minimum for options approval? None"; margin 6.50% base confirmed as of 2026-09-30; ACATS inbound free for non-US accounts.

### Inferences
- For a 20 USD account the best route is LCT in the bank-account currency: about 0.30 USD in or out (1.5% of 20 USD), versus a USD SWIFT wire where the 35 USD outbound fee exceeds the balance and the Polish bank's own outgoing-USD and intermediary fees are unknown. A withdrawal by wire is effectively unusable; leaving the money in the account or withdrawing via LCT is the only sensible exit.
- Whether LCT has a minimum amount is not published; the learn guide says only that US-dollar funding can start "as little as $1" after approval.
- Revolut cannot be the source for LCT; Wise via ACH/Plaid or a Polish bank IBAN are the practical sources (Wise route reported by a non-resident, secondary only).

### Gaps
- Polish-bank-side costs for a USD SWIFT wire, and PLN vs EUR handling inside CurrencyCloud for Polish IBANs, were not verified.
- No primary list of tax documents Alpaca provides to non-US clients beyond the 1042-S article (annual statements, CSV export formats not confirmed; the account dashboard offers "account statements, and tax documents" per the 2020 FAQ).
- Whether the FINRA TAF is currently waived (spec Q4) was not checked here.

---

## Verdict: can a Polish tax resident open an individual live Alpaca account and trade US stocks through the API in 2026?

### Takeaway
Probably yes, but unconfirmed: no public Alpaca or community source names Poland, so treat it as "plausible, ~60% by judgement, confidence LOW-to-MEDIUM [L/M]" until Alpaca support confirms in writing. The decisive checks are (1) whether Poland is in the "Country of Tax Residence" list at signup and survives the live application without a REJECTED status, and (2) a written reply to the drafted support email.

### Cited Findings
Dated evidence table (newest first)

| Date | Source | Who / what | Finding | Bearing on Poland | Conf. |
|---|---|---|---|---|---|
| 2026-10-21 | [amicobot.it](https://amicobot.it/blog/alpaca-trading/) | Italian review (no account opened) | Individual accounts sit with Alpaca Securities LLC, not on ESMA register; no public country list; fees from Oct-12 schedule | Indirect, EU analogue | M |
| 2026-10-12 | [Fee schedule PDF](https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf) | Alpaca official | Intl wire out 35 USD, LCT 1.5% max 40 USD, daily-aggregated round-up reg fees | Funding cost for 20 USD account | H |
| 2026-09-30 | [Forum staff reply](https://forum.alpaca.markets/t/no-reply-from-customer-service/19766) | Alpaca staff to Singapore resident | Non-US individuals accepted; held by Alpaca Securities LLC; W-8BEN every 3 years; automated API trading not itself non-retail | Shows model is live; Poland not named | H (general) |
| 2026-07-26 | [Forum staff reply](https://forum.alpaca.markets/t/alpaca-forum-account-hijacking/19370/10) | Alpaca staff to Brazil resident | Brazil tax residency unsupported; account shows REJECTED, cannot fund | Shows country-level rejection mechanism | H |
| 2026-07-07 | [Alpaca blog/Business Wire](https://alpaca.markets/blog/alpaca-completes-eea-passporting-to-29-countries-expanding-access-to-regulated-investment-services-across-europe/) | Alpaca official | EEA passport incl. Poland, for fintechs/institutions/enterprise partners | B2B only; no retail route | H |
| 2026-06-10 / 2026-06-29 | [Canada](https://forum.alpaca.markets/c/getting-started-with-alpaca/19.md?page=1), [Indonesia](https://forum.alpaca.markets/t/country-of-tax-residence-indonesia/11836) | Forum | Canada not in list; Indonesia still unsupported | Known exclusions do not include any EU state | M |
| 2026-04-07 | [BrokerChooser review](https://brokerchooser.com/broker-reviews/alpaca-trading-review) | Review site | "Available in the United States"; "Open to clients worldwide"; wire out $25/$50 | Ambiguous | L |
| 2026-03-11 | [Learn: non-US guide](https://alpaca.markets/learn/live-trading-account-non-us/) | Alpaca official | 195+ countries; Portugal tax resident OK; Canada not; $1 funding; no Poland | Closest official evidence for EU | H |
| 2026-02 | [Countries page](https://alpaca.markets/support/countries-alpaca-is-available) | Alpaca official | No list; "contact support" | None | H |
| 2026-01-26 | [Learn: funding guide](https://alpaca.markets/learn/fund-live-trading-account/) | Alpaca official | USD-only wires, CurrencyCloud LCT 1.5% cap 40 USD | Funding route | H |
| ~2026-01-05 | [Reddit snapshot](https://reddit.sentinel-team.org/posts/1tvoxp3/snapshots/2026-06-03T20%3A41%3A04.265276Z) | Anonymous Spain-based dev | Claims Alpaca retail API US-only, Europe = B2B | Contradicted by official pages | L |
| 2025-09-11 | [BrokerChooser PL](https://brokerchooser.com/pl/best-brokers/best-brokers-for-algo-trading) | Polish-language ranking | Alpaca #1 for algos; no Poland-availability claim | Weak | L |
| 2025-04-25 | [CNMV register](https://www.cnmv.es/portal/consultas/esi/esis?lang=en&nif=B75852608&vista=17) | Regulator | Alpaca Europe A.V. registered (no. 328) | B2B entity only | H |
| 2024-10 to 2025-06 | [Trustpilot via amicobot](https://amicobot.it/blog/alpaca-trading/) | Non-US users, country unstated | Live non-US usage (3,000+ trades, withdrawals next day) | Not Polish | L |
| 2021-03-01 | [KNF komunikat](https://www.knf.gov.pl/komunikacja/komunikaty?articleId=72670&p_id=18) | Polish regulator | Non-EU firms: only via branch, except exclusive client initiative; abuse via "I agree" pop-ups flagged | Legal frame for Polish retail | H |
| 2020-05/06 | [Forum](https://forum.alpaca.markets/t/alpaca-as-a-non-us-citizen/1523) | Users | Sweden, France, Germany, Netherlands selectable; Czech, Belgium not | Old; Poland not mentioned | L |
| 2020-07-30 | [Alpaca beta blog](https://alpaca.markets/blog/non-us-live-trading-beta/) | Alpaca | Non-solicitation agreement, $30,000 minimum (since lifted) | Origin of reverse-solicitation framing | L (old) |

### Inferences
- Probability judgement (my estimate, not sourced): about 60% that a Polish tax resident can open and fund an individual live account today; about 25% that Poland is silently unsupported or later REJECTED (the Brazil pattern); about 15% that the account opens but practical friction (KYC with Polish PESEL/NIP, funding rail, support delays) makes a 20 USD live account not worth running. No Polish first-hand data to move these numbers.
- If it opens, the API path is the same as the US one (`paper=False` in alpaca-py); the Alpaca Europe B2B stack is not involved.
- Cost reality for 20 USD: trading itself is commission-free, but money movement dominates (about 0.30 USD in/out via LCT, 35 USD for an international wire out). Support quality for non-US funding issues is a recurring complaint in Sept 2026.

### Gaps
- Close the gap with: (a) send the drafted email, adding explicit questions "Is tax residency Poland on your supported list for individual live accounts at Alpaca Securities LLC today?", "Which Tax ID do you accept for Poland (PESEL or NIP)?", "Is a non-solicitation / own-initiative declaration required?" and "Which outbound international wire fee applies: the 35 USD in the 2026-10-12 schedule or the 50 USD in the funding guide?"; (b) the owner (not this agent) checks the Country of Tax Residence dropdown; (c) a human search of Reddit PL/Wykop/Bankier/Facebook groups for "alpaca".
- Unresolved: Alpaca's actual list, Polish KYC document handling, KNF stance on Alpaca specifically, continuity risk if Alpaca tightens EEA retail onboarding after its EEA passport launch.
