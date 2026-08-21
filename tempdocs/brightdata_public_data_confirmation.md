# 7. Confirmation That All Scraped Data Is Publicly Available

**Yes. All data collected by GapRadar through Bright Data comes from publicly accessible web sources. GapRadar does not scrape private, login-gated, account-only, or paywalled content, and it does not attempt to bypass access controls.**

GapRadar currently uses Bright Data against three public-data surfaces.

---

## 1. Razorpay Fix My Itch — Public Webpage

GapRadar's Discovery Mode uses:

**https://razorpay.com/m/fix-my-itch/**

The page is publicly accessible without:

- Logging in
- Creating an account
- Entering credentials
- Paying for access

Our custom Bright Data Scraper Studio collector only extracts information visible on this public page, such as:

- Problem statements
- Descriptions
- Industries
- Published scores
- Source information

### Flow

```text
Public Razorpay Fix My Itch page
        ↓
Bright Data Scraper Studio
        ↓
GapRadar
```

No authenticated or private Razorpay data is accessed.

---

## 2. arXiv — Public Academic Search and Results Pages

Investigation Mode uses our custom Bright Data Scraper Studio collector against:

**https://arxiv.org/search/**

arXiv search pages and research metadata are publicly accessible.

GapRadar uses public information such as:

- Paper titles
- Authors
- Abstracts
- Paper URLs
- Search-result metadata

GapRadar generates an arXiv search URL, Bright Data visits that public URL, and our custom scraper extracts the research results.

No:

- University login
- Paid journal subscription
- Institutional authentication
- Private research repository

is involved.

### Flow

```text
Public arXiv search
        ↓
Bright Data Scraper Studio
        ↓
Research Evidence in GapRadar
```

---

## 3. Google Search Through Bright Data SERP API

For demand evidence and competitor discovery, GapRadar uses the **Bright Data SERP API**.

GapRadar consumes structured organic search-result information returned by Bright Data, such as:

- Result title
- URL/domain
- Public search snippet
- Ranking/result metadata

Importantly, GapRadar's SERP integration **does not automatically open or scrape the websites returned by those search results**.

Therefore, if a search result points to a website that later requires authentication or contains paywalled content, GapRadar does not bypass that restriction or scrape the protected content.

### Flow

```text
Public Google search results
        ↓
Bright Data SERP API
        ↓
GapRadar
```

It is **not**:

```text
Google result
        ↓
Bypass login/paywall
        ↓
Scrape protected content
```

---

# What GapRadar Does Not Scrape

GapRadar does **not** collect:

- Private user accounts or profiles
- Login-gated dashboards
- Authenticated application data
- Subscription-only or paywalled article bodies
- Private databases
- Content requiring credentials
- Data obtained by bypassing access controls

---

# Final Confirmation

> **Yes. All web data collected by GapRadar through Bright Data is obtained from publicly accessible sources. Our custom Scraper Studio collectors operate on the public Razorpay Fix My Itch and arXiv pages, while Bright Data SERP API provides public organic search-result metadata for Investigation Mode. GapRadar does not scrape private, login-gated, account-restricted, or paywalled content, and it does not attempt to bypass access controls.**
